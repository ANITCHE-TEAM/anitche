"""Cycle de vie d'une commande : seul point d'entrée pour changer son statut.

Table des transitions (docs/MODULE_COMMANDES.md) :

    creee       → confirmee   système (paiement en ligne validé)
    creee       → annulee     client, expiration, boutique indisponible, administration
    confirmee   → preparation vendeur de la boutique
    confirmee   → annulee     client (pas encore en préparation), administration
    preparation → expediee    synchronisé depuis la livraison (expédiée)
    preparation → annulee     administration
    expediee    → livree      synchronisé depuis la livraison (livrée)
    expediee    → annulee     administration, livraison définitivement échouée
                              (apps.livraison.services.abandonner_livraison)

Tout le reste est refusé (TransitionImpossible) : retour arrière, saut
d'étape, sortie d'un état final. Chaque transition est un UPDATE
conditionnel sur le statut attendu : deux requêtes simultanées ne peuvent
pas l'appliquer deux fois (une annulation ne restitue le stock qu'une fois).
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

from django.conf import settings
from django.db import transaction
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
    Statut.EXPEDIEE: {Statut.LIVREE, Statut.ANNULEE},
    Statut.LIVREE: set(),
    Statut.ANNULEE: set(),
}

#: Depuis quels statuts chaque motif permet d'annuler.
ANNULATION_POSSIBLE_DEPUIS = {
    Motif.CLIENT: {Statut.CREEE, Statut.CONFIRMEE},
    Motif.EXPIRATION: {Statut.CREEE},
    Motif.BOUTIQUE_INDISPONIBLE: {Statut.CREEE},
    Motif.ADMINISTRATION: {Statut.CREEE, Statut.CONFIRMEE, Statut.PREPARATION},
    Motif.LIVRAISON_ECHOUEE: {Statut.EXPEDIEE},
}

#: Statut de livraison → statut de commande à atteindre.
SYNCHRONISATION_LIVRAISON = {
    'expediee': Statut.EXPEDIEE,
    'livree': Statut.LIVREE,
}


class TransitionImpossible(Exception):
    """La commande n'est pas dans un statut qui permet cette transition."""


# =====================================================================
# MONTANTS DU CHECKOUT
# =====================================================================

class CheckoutRefuse(Exception):
    """Panier ou coupon qui ne permet pas de commander (400). `detail` :
    message ou dictionnaire {champ: message}, repris tel quel par la vue."""

    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


@dataclass
class LotBoutique:
    """Une future commande : les articles d'une boutique et leurs montants."""

    boutique: object
    items: list
    montant_articles: Decimal
    remise: Decimal = Decimal("0")
    frais_livraison: Decimal = Decimal("0")
    livraison_offerte: bool = False
    frais_livraison_vendeur: Decimal = Decimal("0")

    @property
    def montant_total(self):
        return self.montant_articles - self.remise + self.frais_livraison


@dataclass
class Checkout:
    lots: list = field(default_factory=list)
    # Zone tarifaire déduite de la commune (figée dans GroupeCommande).
    zone: str = ""

    def _somme(self, attribut):
        return sum((getattr(lot, attribut) for lot in self.lots), Decimal("0"))

    @property
    def total_articles(self):
        return self._somme("montant_articles")

    @property
    def total_remise(self):
        return self._somme("remise")

    @property
    def total_frais_livraison(self):
        return self._somme("frais_livraison")

    @property
    def total_a_payer(self):
        return self._somme("montant_total")


def trouver_coupon(code, verrouiller=False):
    """Coupon saisi (insensible à la casse), ou None sans code. Verrouillé
    pendant la validation du panier : il ne peut être consommé qu'une fois."""
    from apps.fidelite.models import CouponReduction

    code = str(code or "").strip()
    if not code:
        return None
    coupons = CouponReduction.objects.select_for_update() if verrouiller else CouponReduction.objects
    coupon = coupons.filter(code__iexact=code).first()
    if coupon is None:
        raise CheckoutRefuse({"coupon_code": f"Le code promo '{code}' n'existe pas."})
    return coupon


def calculer_checkout(items, adresse, client, coupon=None):
    """Montants de chaque future commande (une par boutique), calculés côté
    serveur. Seule source de ces montants : la validation du panier les
    enregistre, la simulation les affiche avant le paiement.

    - remise du coupon : sur les articles seulement (jamais sur les frais),
      arrondie au franc inférieur et répartie en francs entiers au prorata
      de chaque boutique, la dernière absorbant le reste ;
    - frais de livraison : un tarif par commande selon la commune, dont la
      zone est déduite par le serveur (apps.livraison.frais), payés par le
      client ou offerts par la boutique.

    Lève CheckoutRefuse (panier vide, articles indisponibles, coupon non
    valable) ou apps.livraison.frais.TarifIntrouvable (configuration).
    """
    from apps.livraison.frais import frais_de_la_commande, tarif_applicable

    if not items:
        raise CheckoutRefuse("Le panier est vide.")

    # Une variante retirée de la vente, un produit désactivé, ou une
    # boutique suspendue entre l'ajout au panier et le paiement ne peuvent
    # jamais être commandés (PanierItem.est_disponible, même règle que
    # l'API panier). Toutes les lignes concernées sont listées, pour que
    # le client les retire en une fois.
    items_non_achetables = [item for item in items if not item.est_disponible]
    if items_non_achetables:
        libelles = ", ".join(
            f"{item.variante.produit.nom} ({item.variante.nom})" for item in items_non_achetables
        )
        raise CheckoutRefuse(
            f"Ces articles ne sont plus disponibles à la vente : {libelles}. "
            "Retirez-les du panier pour valider la commande."
        )

    lots = {}
    for item in items:
        boutique = item.variante.produit.boutique
        lot = lots.setdefault(boutique, LotBoutique(boutique=boutique, items=[], montant_articles=Decimal("0.00")))
        lot.items.append(item)
        lot.montant_articles += item.prix_unitaire * item.quantite
    checkout = Checkout(lots=list(lots.values()))

    if coupon is not None:
        total_articles = checkout.total_articles
        valide, message = coupon.est_valide_pour(client, total_articles)
        if not valide:
            raise CheckoutRefuse({"coupon_code": message})
        remise_totale = coupon.calculer_remise(total_articles)
        remise_cumulee = Decimal("0")
        for index, lot in enumerate(checkout.lots):
            if index == len(checkout.lots) - 1:
                lot.remise = remise_totale - remise_cumulee
            else:
                lot.remise = (remise_totale * lot.montant_articles / total_articles).quantize(
                    Decimal("1"), rounding=ROUND_DOWN,
                )
                remise_cumulee += lot.remise

    applicable = tarif_applicable(adresse["commune"])
    checkout.zone = applicable.zone
    for lot in checkout.lots:
        for attribut, valeur in frais_de_la_commande(lot.boutique, applicable.tarif).items():
            setattr(lot, attribut, valeur)
    return checkout


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
    """creee → confirmee (paiement en ligne validé).

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
    from apps.fidelite.services import ouvrir_gain
    from apps.paiements.reversements import ouvrir_retractation

    cible = SYNCHRONISATION_LIVRAISON.get(statut_livraison)
    if cible is None:
        return
    _transitionner(commande, cible, {statut for statut, suivants in TRANSITIONS.items() if cible in suivants})
    if cible == Statut.LIVREE:
        # Livraison confirmée : le délai de rétractation avant reversement
        # au vendeur commence (apps.paiements.reversements), et les points
        # de fidélité de la commande sont « en attente » jusqu'à sa fin.
        ouvrir_retractation(commande)
        ouvrir_gain(commande)


def annuler_commande(commande, motif, acteur=None, commentaire="Commande annulée."):
    """Annule la commande, restitue son stock une seule fois, crée le
    remboursement des paiements déjà encaissés, annule le reversement et
    passe la fiche de livraison « annulée » (historique : `acteur`,
    `commentaire`)."""
    from apps.fidelite.services import restituer_coupon
    from apps.livraison.services import annuler_livraison_de
    from apps.notifications.services import notifier_annulation_commande
    from apps.paiements.services import traiter_paiements_apres_annulation

    with transaction.atomic():
        # Livraison d'abord (verrou de la fiche avant la commande, même ordre
        # que les transitions de livraison) : une livraison déjà partie
        # refuse l'annulation, et tout est annulé avec elle.
        annuler_livraison_de(commande, acteur=acteur, commentaire=commentaire)
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
        # Tout le checkout annulé : le coupon utilisé est rendu (s'il n'a pas expiré).
        restituer_coupon(commande)
        notifier_annulation_commande(commande, etait_payee=est_payee(commande))

    logger.info("Commande %s annulée (%s).", commande.numero_commande, motif)


def est_payee(commande):
    """Un paiement couvrant cette commande a-t-il été encaissé ?"""
    from apps.paiements.models import Paiement

    return commande.paiements_couvrants.filter(statut=Paiement.Statut.VALIDE).exists()


def expirer_commandes_impayees(maintenant=None):
    """Annule les commandes restées « créées » (donc non payées : toute
    commande se paie en ligne, sans exception) au-delà du délai
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
