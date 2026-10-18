"""Règles des avis (docs/MODULE_AVIS.md).

Un client note un produit qu'il a reçu : l'éligibilité repose sur l'ACHAT
(ligne d'une commande livrée du client), jamais sur le rôle. Seul le
propriétaire de la boutique ne peut pas noter ses propres produits.

Verrous : toute écriture verrouille la ligne Produit puis la ligne Boutique
(toujours dans cet ordre) avant de recalculer les agrégats. Deux avis
simultanés sur le même produit sont ainsi appliqués l'un après l'autre, et
chacun recalcule sur les avis déjà validés de l'autre : la note est juste.
"""

import logging
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, OuterRef, Q, Avg
from django.utils import timezone

from apps.catalogue.models import Produit
from apps.commandes.models import Commande, CommandeItem
from apps.vendeurs.models import Boutique

from .models import Avis, DecisionModeration, ReponseVendeur, SignalementAvis

logger = logging.getLogger(__name__)

Statut = Avis.Statut


class AvisRefuse(Exception):
    """Action refusée : message pour l'utilisateur, code HTTP."""

    def __init__(self, message, code_http=400):
        super().__init__(message)
        self.message = message
        self.code_http = code_http


# =====================================================================
# DÉLAIS ET ÉLIGIBILITÉ
# =====================================================================

def date_livraison_de(commande):
    """Date de remise du colis (fiche de livraison), ou None."""
    livraison = getattr(commande, "livraison", None)
    return livraison.date_livraison if livraison is not None else None


def date_limite_avis(commande):
    date_livraison = date_livraison_de(commande)
    if date_livraison is None:
        return None
    return date_livraison + timedelta(days=settings.AVIS_DELAI_JOURS)


def date_limite_modification(avis):
    return avis.date_creation + timedelta(days=settings.AVIS_DELAI_MODIFICATION_JOURS)


def articles_a_noter(client, maintenant=None):
    """Lignes de commande que le client peut encore noter : commande livrée
    dans le délai, produit pas encore noté par lui (même supprimé), hors
    produits de sa propre boutique."""
    maintenant = maintenant or timezone.now()
    limite = maintenant - timedelta(days=settings.AVIS_DELAI_JOURS)
    deja_note = Avis.objects.filter(client=client, produit_id=OuterRef("variante__produit_id"))
    return (
        CommandeItem.objects
        .filter(
            commande__client=client,
            commande__status=Commande.Status.LIVREE,
            commande__livraison__date_livraison__gte=limite,
        )
        .exclude(Exists(deja_note))
        .exclude(variante__produit__boutique__proprietaire=client)
        .select_related("commande__livraison", "commande__boutique", "variante__produit__boutique")
        .order_by("-commande__livraison__date_livraison", "id")
    )


def _verifier_eligibilite(client, article, maintenant):
    commande = article.commande
    produit = article.variante.produit
    if produit.boutique.proprietaire_id == client.pk:
        raise AvisRefuse("Vous ne pouvez pas noter les produits de votre propre boutique.", 403)
    if commande.status != Commande.Status.LIVREE:
        raise AvisRefuse("Seul un article d'une commande livrée peut être noté.")
    limite = date_limite_avis(commande)
    if limite is None:
        raise AvisRefuse("Date de livraison inconnue : contactez le support.")
    if maintenant > limite:
        raise AvisRefuse(
            f"Le délai pour noter ce produit ({settings.AVIS_DELAI_JOURS} jours après la livraison) est écoulé."
        )


# =====================================================================
# AGRÉGATS
# =====================================================================

def _verrouiller(produit_id, boutique_id):
    """Produit puis Boutique : ordre unique, pas d'interblocage entre écritures."""
    Produit.objects.select_for_update().get(pk=produit_id)
    Boutique.objects.select_for_update().get(pk=boutique_id)


def recalculer_agregats(produit_id, boutique_id):
    """Note moyenne et nombre d'avis du produit, avis et avis positifs de la
    boutique, recalculés depuis les avis publiés. À appeler dans la
    transaction qui a verrouillé le produit et la boutique."""
    publies = Avis.objects.filter(statut=Statut.PUBLIE)

    produit = publies.filter(produit_id=produit_id).aggregate(nombre=Count("id"), moyenne=Avg("note"))
    nombre = produit["nombre"]
    moyenne = Decimal(str(produit["moyenne"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if nombre else None
    Produit.objects.filter(pk=produit_id).update(note_moyenne=moyenne, nombre_avis=nombre)

    boutique = publies.filter(boutique_id=boutique_id).aggregate(
        nombre=Count("id"),
        positifs=Count("id", filter=Q(note__gte=Avis.NOTE_POSITIVE_MIN)),
    )
    Boutique.objects.filter(pk=boutique_id).update(
        nombre_avis=boutique["nombre"], nombre_avis_positifs=boutique["positifs"],
    )


# =====================================================================
# ÉCRITURES
# =====================================================================

def creer_avis(client, commande_item_id, note, commentaire=""):
    """Publie l'avis du client sur le produit d'une ligne de commande livrée.

    Lève AvisRefuse : 404 (ligne inconnue ou d'un autre client), 403
    (propre boutique), 400 (commande non livrée, délai écoulé), 409 (produit
    déjà noté par ce client, supprimé compris)."""
    maintenant = timezone.now()
    with transaction.atomic():
        article = (
            CommandeItem.objects
            .select_related("commande__livraison", "variante__produit__boutique")
            .filter(pk=commande_item_id, commande__client=client)
            .first()
        )
        if article is None:
            raise AvisRefuse("Article de commande introuvable.", 404)
        produit = article.variante.produit

        _verrouiller(produit.pk, produit.boutique_id)
        _verifier_eligibilite(client, article, maintenant)
        if Avis.objects.filter(client=client, produit=produit).exists():
            raise AvisRefuse("Vous avez déjà donné votre avis sur ce produit.", 409)

        try:
            with transaction.atomic():
                avis = Avis.objects.create(
                    client=client, produit=produit, boutique_id=produit.boutique_id,
                    commande_item=article, note=note, commentaire=commentaire,
                )
        except IntegrityError:
            raise AvisRefuse("Vous avez déjà donné votre avis sur ce produit.", 409)
        recalculer_agregats(produit.pk, produit.boutique_id)

    logger.info("Avis %s publié (produit_id=%s, note=%s).", avis.pk, produit.pk, note)
    return avis


def _avis_de_l_auteur_verrouille(avis_id, client):
    """Avis du client, sous verrou (produit, boutique, puis avis), ou 404.
    Un avis supprimé n'existe plus pour son auteur."""
    avis = Avis.objects.filter(pk=avis_id, client=client).exclude(statut=Statut.SUPPRIME).first()
    if avis is None:
        raise AvisRefuse("Avis introuvable.", 404)
    _verrouiller(avis.produit_id, avis.boutique_id)
    avis = Avis.objects.select_for_update().filter(pk=avis_id).exclude(statut=Statut.SUPPRIME).first()
    if avis is None:
        raise AvisRefuse("Avis introuvable.", 404)
    return avis


def modifier_avis(avis_id, client, note=None, commentaire=None):
    """Modifie la note et/ou le commentaire, par l'auteur, tant que l'avis
    est publié et dans le délai de modification."""
    with transaction.atomic():
        avis = _avis_de_l_auteur_verrouille(avis_id, client)
        if avis.statut != Statut.PUBLIE:
            raise AvisRefuse("Cet avis a été masqué par la modération : il ne peut plus être modifié.", 403)
        if timezone.now() > date_limite_modification(avis):
            raise AvisRefuse(
                f"Un avis ne se modifie que pendant {settings.AVIS_DELAI_MODIFICATION_JOURS} jours après sa publication."
            )
        if note is not None:
            avis.note = note
        if commentaire is not None:
            avis.commentaire = commentaire
        avis.est_modifie = True
        avis.save(update_fields=["note", "commentaire", "est_modifie", "date_modification"])
        recalculer_agregats(avis.produit_id, avis.boutique_id)
    return avis


def supprimer_avis(avis_id, client):
    """Suppression douce par l'auteur : l'avis sort des agrégats et le client
    ne peut plus noter ce produit."""
    with transaction.atomic():
        avis = _avis_de_l_auteur_verrouille(avis_id, client)
        if avis.statut != Statut.PUBLIE:
            raise AvisRefuse("Cet avis a été masqué par la modération : il ne peut pas être supprimé.", 403)
        avis.statut = Statut.SUPPRIME
        avis.save(update_fields=["statut", "date_modification"])
        recalculer_agregats(avis.produit_id, avis.boutique_id)
    logger.info("Avis %s supprimé par son auteur.", avis.pk)


# =====================================================================
# RÉPONSE DU VENDEUR
# =====================================================================

def _avis_publie_de_la_boutique_du_vendeur(avis_id, vendeur):
    """Avis publié sur un produit de la boutique du vendeur, sous verrou
    (la modération ne peut pas le masquer pendant l'écriture de la réponse).

    404 : avis inconnu, masqué ou supprimé. 403 : avis d'une autre boutique,
    ou boutique suspendue (gelée côté vendeur)."""
    avis = (
        Avis.objects.select_for_update(of=("self",))
        .select_related("boutique")
        .filter(pk=avis_id, statut=Statut.PUBLIE)
        .first()
    )
    if avis is None:
        raise AvisRefuse("Avis introuvable.", 404)
    if avis.boutique.proprietaire_id != vendeur.pk:
        raise AvisRefuse("Cet avis ne concerne pas votre boutique.", 403)
    if avis.boutique.est_suspendue:
        raise AvisRefuse("Votre boutique est suspendue par l'administration : aucune réponse n'est possible.", 403)
    return avis


def repondre_a_un_avis(avis_id, vendeur, contenu):
    """Première réponse du propriétaire de la boutique à un avis publié.
    409 si l'avis a déjà une réponse (la modifier avec `modifier_reponse`)."""
    with transaction.atomic():
        avis = _avis_publie_de_la_boutique_du_vendeur(avis_id, vendeur)
        try:
            with transaction.atomic():
                return ReponseVendeur.objects.create(avis=avis, auteur=vendeur, contenu=contenu)
        except IntegrityError:
            raise AvisRefuse("Vous avez déjà répondu à cet avis : modifiez votre réponse.", 409)


def modifier_reponse(avis_id, vendeur, contenu):
    """Modifie la réponse existante ; `date_modification` est mise à jour."""
    with transaction.atomic():
        avis = _avis_publie_de_la_boutique_du_vendeur(avis_id, vendeur)
        reponse = ReponseVendeur.objects.filter(avis=avis).first()
        if reponse is None:
            raise AvisRefuse("Aucune réponse à modifier pour cet avis.", 404)
        reponse.contenu = contenu
        reponse.save(update_fields=["contenu", "date_modification"])
        return reponse


# =====================================================================
# SIGNALEMENT
# =====================================================================

def signaler_avis(avis_id, utilisateur, motif, commentaire=""):
    """Signale un avis publié. 404 : avis inconnu, masqué ou supprimé (le
    public ne le voit pas). 403 : son propre avis. 409 : déjà signalé par cet
    utilisateur."""
    avis = Avis.objects.filter(pk=avis_id, statut=Statut.PUBLIE).first()
    if avis is None:
        raise AvisRefuse("Avis introuvable.", 404)
    if avis.client_id == utilisateur.pk:
        raise AvisRefuse("Vous ne pouvez pas signaler votre propre avis.", 403)
    try:
        with transaction.atomic():
            return SignalementAvis.objects.create(avis=avis, auteur=utilisateur, motif=motif, commentaire=commentaire)
    except IntegrityError:
        raise AvisRefuse("Vous avez déjà signalé cet avis.", 409)


# =====================================================================
# MODÉRATION
# =====================================================================

def _avis_verrouille_pour_moderation(avis_id):
    """Avis sous verrou (produit, boutique, puis avis), ou 404."""
    avis = Avis.objects.filter(pk=avis_id).first()
    if avis is None:
        raise AvisRefuse("Avis introuvable.", 404)
    _verrouiller(avis.produit_id, avis.boutique_id)
    return Avis.objects.select_for_update().get(pk=avis_id)


def masquer_avis(avis_id, moderateur, motif):
    """Masque un avis publié : il sort du public et des moyennes (recalculées).
    Tous ses signalements ouverts sont acceptés par cette même décision."""
    with transaction.atomic():
        avis = _avis_verrouille_pour_moderation(avis_id)
        if avis.statut != Statut.PUBLIE:
            raise AvisRefuse("Seul un avis publié peut être masqué.", 409)
        avis.statut = Statut.MASQUE
        avis.save(update_fields=["statut", "date_modification"])
        decision = DecisionModeration.objects.create(
            avis=avis, moderateur=moderateur, type_decision=DecisionModeration.TypeDecision.MASQUER, motif=motif,
        )
        SignalementAvis.objects.filter(avis=avis, statut=SignalementAvis.Statut.OUVERT).update(
            statut=SignalementAvis.Statut.ACCEPTE, decision=decision, date_traitement=timezone.now(),
        )
        recalculer_agregats(avis.produit_id, avis.boutique_id)
    logger.info("Avis %s masqué par %s.", avis.pk, moderateur.pk)
    return decision


def retablir_avis(avis_id, moderateur, motif):
    """Rétablit un avis masqué : il revient dans le public et les moyennes."""
    with transaction.atomic():
        avis = _avis_verrouille_pour_moderation(avis_id)
        if avis.statut != Statut.MASQUE:
            raise AvisRefuse("Seul un avis masqué peut être rétabli.", 409)
        avis.statut = Statut.PUBLIE
        avis.save(update_fields=["statut", "date_modification"])
        decision = DecisionModeration.objects.create(
            avis=avis, moderateur=moderateur, type_decision=DecisionModeration.TypeDecision.RETABLIR, motif=motif,
        )
        recalculer_agregats(avis.produit_id, avis.boutique_id)
    logger.info("Avis %s rétabli par %s.", avis.pk, moderateur.pk)
    return decision


def rejeter_signalement(signalement_id, moderateur, motif):
    """Rejette un signalement ouvert : l'avis reste publié."""
    with transaction.atomic():
        signalement = SignalementAvis.objects.select_for_update().filter(pk=signalement_id).first()
        if signalement is None:
            raise AvisRefuse("Signalement introuvable.", 404)
        if signalement.statut != SignalementAvis.Statut.OUVERT:
            raise AvisRefuse("Ce signalement a déjà été traité.", 409)
        decision = DecisionModeration.objects.create(
            avis_id=signalement.avis_id, moderateur=moderateur,
            type_decision=DecisionModeration.TypeDecision.REJETER_SIGNALEMENT, motif=motif,
        )
        signalement.statut = SignalementAvis.Statut.REJETE
        signalement.decision = decision
        signalement.date_traitement = timezone.now()
        signalement.save(update_fields=["statut", "decision", "date_traitement"])
    logger.info("Signalement %s rejeté par %s.", signalement.pk, moderateur.pk)
    return decision
