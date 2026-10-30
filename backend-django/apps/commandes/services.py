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

`changer_statut_commande` est la seule fonction qui écrit le statut d'une
commande, et `creer_commande` la seule qui en crée une : toutes deux écrivent
la ligne CommandeHistorique dans la même transaction. Aucun autre code
(UPDATE en masse, affectation directe) ne doit toucher `Commande.status`.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

from django.conf import settings
from django.db import OperationalError, transaction
from django.utils import timezone

from apps.catalogue.models import Stock

from .models import Commande, CommandeHistorique, GroupeCommande

logger = logging.getLogger(__name__)

Statut = Commande.Status
Motif = Commande.MotifAnnulation
Source = CommandeHistorique.Source

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

#: Source de l'annulation selon son motif. La livraison définitivement échouée
#: est décidée par l'administration (apps.livraison.services.abandonner_livraison).
SOURCE_ANNULATION = {
    Motif.CLIENT: Source.CLIENT,
    Motif.ADMINISTRATION: Source.ADMIN,
    Motif.LIVRAISON_ECHOUEE: Source.ADMIN,
    Motif.EXPIRATION: Source.SYSTEME,
    Motif.BOUTIQUE_INDISPONIBLE: Source.SYSTEME,
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

    Lève CheckoutRefuse (panier vide, article de sa propre boutique, articles
    indisponibles, coupon non valable) ou apps.livraison.frais.TarifIntrouvable
    (configuration).

    La validation appelle cette fonction dans sa transaction, panier verrouillé
    et avant les verrous de stock : le refus de l'auto-achat y est donc pris
    sur l'état verrouillé du panier, et la simulation répond comme la validation.
    """
    from apps.livraison.frais import frais_de_la_commande, tarif_applicable
    from apps.panier.services import (
        CODE_AUTO_ACHAT_INTERDIT,
        est_article_de_sa_boutique,
    )

    if not items:
        raise CheckoutRefuse("Le panier est vide.")

    # Un vendeur ne commande pas dans sa propre boutique (ventes, avis et
    # niveau vendeur gonflés). Même règle et même code qu'à l'ajout au panier ;
    # toutes les lignes concernées sont listées pour être retirées en une fois.
    items_propre_boutique = [item for item in items if est_article_de_sa_boutique(item.variante, client.pk)]
    if items_propre_boutique:
        libelles = ", ".join(
            f"{item.variante.produit.nom} ({item.variante.nom})" for item in items_propre_boutique
        )
        raise CheckoutRefuse({
            "detail": (
                f"Vous ne pouvez pas acheter les articles de votre propre boutique : {libelles}. "
                "Retirez-les du panier pour valider la commande."
            ),
            "code": CODE_AUTO_ACHAT_INTERDIT,
        })

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


def _tracer(commande, ancien_statut, nouveau_statut, source, auteur, motif=""):
    CommandeHistorique.objects.create(
        commande=commande, ancien_statut=ancien_statut, nouveau_statut=nouveau_statut,
        source=source, auteur=auteur, motif=motif,
    )


def creer_commande(auteur, **champs):
    """Crée une commande (statut « créée ») et sa première ligne d'historique,
    sans ancien statut, dans la même transaction. `auteur` : le client qui valide
    son panier."""
    with transaction.atomic():
        commande = Commande.objects.create(**champs)
        _tracer(commande, "", commande.status, Source.CLIENT, auteur)
    return commande


def changer_statut_commande(commande, nouveau_statut, depuis, *, source, auteur=None, motif="", **champs):
    """Seule écriture de `Commande.status` : UPDATE sous verrou qui n'agit que
    si la commande est encore dans l'un des statuts `depuis` (restreints à
    ceux que la table autorise), et ligne d'historique dans la même transaction
    (`source`, `auteur` : compte à l'origine, None pour le système ; `motif` :
    code court). `champs` : colonnes à écrire en même temps (motif_annulation).

    Lève TransitionImpossible, sans rien écrire, quand la transition n'est pas
    permise, y compris vers le statut déjà atteint (aucune ligne d'historique)."""
    autorises = {statut for statut in depuis if nouveau_statut in TRANSITIONS[statut]}
    with transaction.atomic():
        # Le statut lu sous verrou est l'ancien statut de la ligne d'historique.
        ancien_statut = Commande.objects.select_for_update().values_list("status", flat=True).get(pk=commande.pk)
        if ancien_statut in autorises:
            Commande.objects.filter(pk=commande.pk).update(
                status=nouveau_statut, update_at=timezone.now(), **champs,
            )
            _tracer(commande, ancien_statut, nouveau_statut, source, auteur, motif)
    commande.refresh_from_db(fields=['status', 'motif_annulation', 'update_at'])
    if ancien_statut not in autorises:
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
        changer_statut_commande(commande, Statut.CONFIRMEE, {Statut.CREEE}, source=Source.PAIEMENT)
    except TransitionImpossible:
        return False
    logger.info("Commande %s confirmée.", commande.numero_commande)
    return True


def passer_en_preparation(commande, vendeur):
    """confirmee → preparation (vendeur de la boutique)."""
    changer_statut_commande(commande, Statut.PREPARATION, {Statut.CONFIRMEE}, source=Source.VENDEUR, auteur=vendeur)


def synchroniser_depuis_livraison(commande, statut_livraison, acteur=None):
    """Répercute l'expédition et la livraison sur la commande.

    `acteur` : le livreur ou l'administrateur qui a changé la livraison.
    Les autres statuts de livraison (en cours, échouée) ne changent pas la
    commande. Lève TransitionImpossible si la commande n'est pas prête
    (ex : expédier une commande pas encore en préparation, ou annulée).
    """
    from apps.fidelite.services import ouvrir_gain
    from apps.paiements.reversements import ouvrir_retractation

    cible = SYNCHRONISATION_LIVRAISON.get(statut_livraison)
    if cible is None:
        return
    changer_statut_commande(
        commande, cible, {statut for statut, suivants in TRANSITIONS.items() if cible in suivants},
        source=Source.LIVRAISON, auteur=acteur,
    )
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
        livraison = annuler_livraison_de(commande, acteur=acteur, commentaire=commentaire)
        changer_statut_commande(
            commande, Statut.ANNULEE, ANNULATION_POSSIBLE_DEPUIS[motif],
            source=SOURCE_ANNULATION[motif], auteur=acteur, motif=motif, motif_annulation=motif,
        )
        if livraison is None:
            # Aucune fiche à la première lecture, mais une validation de
            # paiement concurrente a pu confirmer la commande et en créer une
            # pendant que l'UPDATE ci-dessus attendait son verrou. Relue une
            # fois la commande verrouillée par cet UPDATE, elle est annulée
            # aussi : une commande annulée n'a jamais de fiche active.
            # Ordre Commande puis Livraison dans ce seul cas : la fiche vient
            # de naître, sans livreur ; seule une assignation par
            # l'administration dans la même milliseconde pourrait la
            # verrouiller (interblocage théorique : un 500, un nouvel essai).
            annuler_livraison_de(commande, acteur=acteur, commentaire=commentaire)

        if commande.groupe_id is not None:
            # Les annulations des commandes sœurs se sérialisent avant de
            # décider si le paiement et le coupon partagés peuvent être finalisés.
            GroupeCommande.objects.select_for_update().get(pk=commande.groupe_id)

        # Seule la requête qui a effectivement annulé arrive ici : le stock
        # n'est restitué qu'une fois, sous le même verrou que le checkout, et
        # dans le même ordre (clés des variantes croissantes : pas
        # d'interblocage avec un checkout ou un retour, quel que soit le plan).
        articles = list(commande.article.all())
        stocks = {
            stock.variante_id: stock
            for stock in Stock.objects.select_for_update().filter(
                variante_id__in=[article.variante_id for article in articles]
            ).order_by("variante_id")
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
    COMMANDE_DELAI_PAIEMENT_MINUTES, et restitue leur stock.

    Une commande qui a un paiement en attente n'expire qu'après avoir
    demandé son état au fournisseur (webhook perdu) : un succès la confirme ;
    un état incertain repousse l'expiration de
    PAIEMENT_RECONCILIATION_DELAI_GRACE_MINUTES
    (apps.paiements.services.reconcilier_avant_expiration)."""
    from apps.paiements.services import reconcilier_avant_expiration

    maintenant = maintenant or timezone.now()
    limite = maintenant - timedelta(minutes=settings.COMMANDE_DELAI_PAIEMENT_MINUTES)
    fin_de_grace = limite - timedelta(minutes=settings.PAIEMENT_RECONCILIATION_DELAI_GRACE_MINUTES)
    # Liste figée avant la boucle : des appels au fournisseur s'y intercalent.
    commandes = list(Commande.objects.filter(status=Statut.CREEE, created_at__lt=limite).order_by('pk'))
    indisponibles = set()
    annulees = 0
    for commande in commandes:
        try:
            # Appel au fournisseur hors transaction, avant tout verrou.
            if not reconcilier_avant_expiration(commande, commande.created_at < fin_de_grace, indisponibles):
                continue
            annuler_commande(commande, Motif.EXPIRATION)
            annulees += 1
        except TransitionImpossible:
            # Payée ou annulée entre la lecture et l'annulation : rien à faire.
            continue
        except OperationalError as erreur:
            # Interblocage ou conflit de sérialisation : la transaction de
            # CETTE commande est annulée ; les suivantes sont traitées, elle
            # le sera à l'exécution suivante.
            logger.warning("Expiration de la commande %s reportée (%s).", commande.numero_commande,
                           str(erreur).splitlines()[0] if str(erreur) else type(erreur).__name__)
            continue
    return annulees
