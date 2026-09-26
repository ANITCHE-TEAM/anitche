"""Cycle de vie d'une commande : seul point d'entrée pour changer son statut.

Table des transitions (docs/MODULE_COMMANDES.md) :

    creee       → confirmee   système (paiement validé, ou paiement à la livraison)
    creee       → annulee     client, expiration, boutique indisponible, administration
    confirmee   → preparation vendeur de la boutique
    confirmee   → annulee     client (pas encore en préparation), administration
    preparation → expediee    synchronisé depuis la livraison (expédiée)
    preparation → annulee     administration
    expediee    → livree      synchronisé depuis la livraison (livrée)

Tout le reste est refusé (TransitionImpossible) : retour arrière, saut
d'étape, sortie d'un état final. Chaque transition est un UPDATE
conditionnel sur le statut attendu : deux requêtes simultanées ne peuvent
pas l'appliquer deux fois (une annulation ne restitue le stock qu'une fois).
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.catalogue.models import Stock

from .models import Commande

logger = logging.getLogger(__name__)

Statut = Commande.Status
Motif = Commande.MotifAnnulation

TRANSITIONS = {
    Statut.CREEE: {Statut.CONFIRMEE, Statut.ANNULEE},
    Statut.CONFIRMEE: {Statut.PREPARATION, Statut.ANNULEE},
    Statut.PREPARATION: {Statut.EXPEDIEE, Statut.ANNULEE},
    Statut.EXPEDIEE: {Statut.LIVREE},
    Statut.LIVREE: set(),
    Statut.ANNULEE: set(),
}

#: Depuis quels statuts chaque motif permet d'annuler.
ANNULATION_POSSIBLE_DEPUIS = {
    Motif.CLIENT: {Statut.CREEE, Statut.CONFIRMEE},
    Motif.EXPIRATION: {Statut.CREEE},
    Motif.BOUTIQUE_INDISPONIBLE: {Statut.CREEE},
    Motif.ADMINISTRATION: {Statut.CREEE, Statut.CONFIRMEE, Statut.PREPARATION},
}

#: Statut de livraison → statut de commande à atteindre.
SYNCHRONISATION_LIVRAISON = {
    'expediee': Statut.EXPEDIEE,
    'livree': Statut.LIVREE,
}


class TransitionImpossible(Exception):
    """La commande n'est pas dans un statut qui permet cette transition."""


def _transitionner(commande, nouveau_statut, depuis, **champs):
    """UPDATE conditionnel : n'agit que si la commande est encore dans l'un
    des statuts `depuis` (restreints à ceux que la table autorise)."""
    autorises = {statut for statut in depuis if nouveau_statut in TRANSITIONS[statut]}
    lignes = Commande.objects.filter(pk=commande.pk, status__in=autorises).update(
        status=nouveau_statut, update_at=timezone.now(), **champs,
    )
    commande.refresh_from_db(fields=['status', 'motif_annulation', 'update_at'])
    if not lignes:
        raise TransitionImpossible(
            f"Impossible de passer la commande {commande.numero_commande} de "
            f"« {commande.get_status_display()} » à « {Statut(nouveau_statut).label} »."
        )


def confirmer_commande(commande):
    """creee → confirmee (paiement validé ou paiement à la livraison).

    Renvoie False, sans rien changer, si la commande n'est plus « créée »
    (déjà confirmée, ou annulée entre-temps) : l'appelant décide alors.
    """
    try:
        _transitionner(commande, Statut.CONFIRMEE, {Statut.CREEE})
    except TransitionImpossible:
        return False
    logger.info("Commande %s confirmée.", commande.numero_commande)
    return True


def passer_en_preparation(commande):
    """confirmee → preparation (vendeur de la boutique)."""
    _transitionner(commande, Statut.PREPARATION, {Statut.CONFIRMEE})


def synchroniser_depuis_livraison(commande, statut_livraison):
    """Répercute l'expédition et la livraison sur la commande.

    Les autres statuts de livraison (en cours, échouée) ne changent pas la
    commande. Lève TransitionImpossible si la commande n'est pas prête
    (ex : expédier une commande pas encore en préparation, ou annulée).
    """
    cible = SYNCHRONISATION_LIVRAISON.get(statut_livraison)
    if cible is None:
        return
    _transitionner(commande, cible, {statut for statut, suivants in TRANSITIONS.items() if cible in suivants})


def annuler_commande(commande, motif):
    """Annule la commande, restitue son stock une seule fois et signale
    les paiements déjà encaissés comme « à rembourser »."""
    from apps.paiements.services import traiter_paiements_apres_annulation

    with transaction.atomic():
        _transitionner(commande, Statut.ANNULEE, ANNULATION_POSSIBLE_DEPUIS[motif], motif_annulation=motif)

        # Seule la requête qui a effectivement annulé arrive ici : le stock
        # n'est restitué qu'une fois, sous le même verrou que le checkout.
        articles = list(commande.article.all())
        stocks = {
            stock.variante_id: stock
            for stock in Stock.objects.select_for_update().filter(
                variante_id__in=[article.variante_id for article in articles]
            )
        }
        for article in articles:
            stock = stocks.get(article.variante_id)
            if stock is not None:
                stock.incrementer(article.quantite)

        traiter_paiements_apres_annulation(commande)

    logger.info("Commande %s annulée (%s).", commande.numero_commande, motif)


def est_payee(commande):
    """Un paiement couvrant cette commande a-t-il été encaissé ?"""
    from apps.paiements.models import Paiement

    filtre = Q(commande=commande)
    if commande.groupe_id:
        filtre |= Q(groupe_commande_id=commande.groupe_id)
    return Paiement.objects.filter(filtre, statut=Paiement.Statut.VALIDE).exists()


def expirer_commandes_impayees(maintenant=None):
    """Annule les commandes restées « créées » (donc non payées : le
    paiement à la livraison les confirme dès son choix) au-delà du délai
    COMMANDE_DELAI_PAIEMENT_MINUTES, et restitue leur stock."""
    limite = (maintenant or timezone.now()) - timedelta(minutes=settings.COMMANDE_DELAI_PAIEMENT_MINUTES)
    annulees = 0
    for commande in Commande.objects.filter(status=Statut.CREEE, created_at__lt=limite).iterator():
        try:
            annuler_commande(commande, Motif.EXPIRATION)
            annulees += 1
        except TransitionImpossible:
            # Payée ou annulée entre la lecture et l'annulation : rien à faire.
            continue
    return annulees
