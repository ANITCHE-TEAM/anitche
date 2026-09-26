"""Points de fidélité et coupons (docs/MODULE_FIDELITE.md).

Points : 1 point par tranche de 1 000 FCFA payés, par commande.

    commande livrée ──► gain « en attente » (fin = livraison + délai de rétractation)
        │   retour remboursé pendant l'attente → points recalculés sur ce qui reste payé
        │   contestation « non reçu » fondée    → gain annulé
        ▼   délai écoulé, aucun retour ni contestation ouverts (tâche périodique)
    gain crédité sur le compte ──► retour remboursé ensuite → reprise plafonnée au solde

Une commande annulée (client, expiration, administration, livraison échouée)
n'est jamais livrée : elle n'a jamais de gain, donc rien à reprendre.
"""

import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Exists, F, OuterRef, Sum
from django.utils import timezone

from .models import CompteFidelite, CouponReduction, GainFidelite, TransactionFidelite, UtilisationCoupon

logger = logging.getLogger(__name__)

TRANCHE_FCFA = Decimal("1000")
Statut = GainFidelite.Statut


def points_pour(montant):
    """1 point par tranche entière de 1 000 FCFA."""
    if not montant or montant <= 0:
        return 0
    return int(Decimal(montant) // TRANCHE_FCFA)


def _compte_de(utilisateur):
    compte, _ = CompteFidelite.objects.get_or_create(utilisateur=utilisateur)
    return compte


# =====================================================================
# GAINS
# =====================================================================

def _deja_credite_au_paiement(commande):
    """Commandes payées avant la refonte : leurs points ont été crédités au
    paiement (référence = paiement). Elles n'ouvrent pas de second gain."""
    references = list(commande.paiements_couvrants.values_list("reference", flat=True))
    return bool(references) and TransactionFidelite.objects.filter(
        compte__utilisateur_id=commande.client_id,
        type_transaction=TransactionFidelite.TypeTransaction.GAIN,
        reference_externe__in=references,
    ).exists()


def ouvrir_gain(commande, moment=None):
    """Commande livrée : points « en attente » jusqu'à la fin du délai de
    rétractation. Idempotent (un gain par commande)."""
    points = points_pour(commande.montant_total)
    if points <= 0 or GainFidelite.objects.filter(commande=commande).exists() or _deja_credite_au_paiement(commande):
        return None
    moment = moment or timezone.now()
    try:
        with transaction.atomic():
            return GainFidelite.objects.create(
                compte=_compte_de(commande.client),
                commande=commande,
                points=points,
                date_disponibilite=moment + timedelta(days=settings.REVERSEMENT_DELAI_RETRACTATION_JOURS),
            )
    except IntegrityError:
        return None


def annuler_gain(commande, motif):
    """Gain en attente annulé (contestation de livraison fondée)."""
    return GainFidelite.objects.filter(commande=commande, statut=Statut.EN_ATTENTE).update(
        statut=Statut.ANNULE, motif_annulation=motif[:255], date_traitement=timezone.now(),
    )


def _montant_rembourse_par_retours(commande):
    from apps.retours.models import DemandeRetour

    return DemandeRetour.objects.filter(
        commande=commande, statut__in=(DemandeRetour.Statut.REMBOURSE, DemandeRetour.Statut.CLOTURE),
    ).aggregate(total=Sum("montant_remboursement"))["total"] or Decimal("0")


def appliquer_retour_rembourse(demande_retour):
    """Retour remboursé : gain en attente recalculé sur ce qui reste payé
    (annulé s'il ne reste rien) ; gain déjà crédité → reprise des points de
    la part remboursée, plafonnée au solde, une seule fois par retour."""
    commande = demande_retour.commande
    with transaction.atomic():
        gain = GainFidelite.objects.select_for_update().filter(commande=commande).first()
        if gain is None:
            return
        if gain.statut == Statut.EN_ATTENTE:
            restant = points_pour(commande.montant_total - _montant_rembourse_par_retours(commande))
            if restant <= 0:
                # Tout est remboursé : le gain est annulé (points gardés pour l'historique).
                gain.statut = Statut.ANNULE
                gain.motif_annulation = f"Retour {demande_retour.numero_retour} remboursé"
                gain.date_traitement = timezone.now()
            else:
                gain.points = min(gain.points, restant)
            gain.save(update_fields=["points", "statut", "motif_annulation", "date_traitement"])
            return
        if gain.statut == Statut.CREDITE:
            _reprendre_points(gain.compte, points_pour(demande_retour.montant_remboursement), demande_retour.numero_retour)


def _reprendre_points(compte, points, reference):
    """Reprise plafonnée au solde : un remboursement dû au client n'est jamais
    bloqué pour des points déjà dépensés (perte assumée, journalisée)."""
    compte = CompteFidelite.objects.select_for_update().get(pk=compte.pk)
    if TransactionFidelite.objects.filter(
        compte=compte, type_transaction=TransactionFidelite.TypeTransaction.REPRISE, reference_externe=reference,
    ).exists():
        return 0
    a_debiter = min(points, compte.solde_points)
    if a_debiter < points:
        logger.warning("Reprise %s : %s pts dus, %s disponibles (déjà dépensés).", reference, points, compte.solde_points)
    if a_debiter <= 0:
        return 0
    compte.debiter_points(
        points=a_debiter,
        description=f"Reprise de points : retour {reference} remboursé",
        reference_externe=reference,
        type_transaction=TransactionFidelite.TypeTransaction.REPRISE,
    )
    return a_debiter


def gains_echus(maintenant=None):
    """Gains en attente dont le délai est écoulé, sans retour ni contestation
    ouverts sur la commande (mêmes conditions que la reprise du reversement)."""
    from apps.livraison.models import ContestationLivraison
    from apps.retours.models import DemandeRetour

    retours_clos = (DemandeRetour.Statut.REJETE, DemandeRetour.Statut.ANNULE,
                    DemandeRetour.Statut.REMBOURSE, DemandeRetour.Statut.CLOTURE)
    retour_ouvert = DemandeRetour.objects.filter(commande=OuterRef("commande")).exclude(statut__in=retours_clos)
    contestation_ouverte = ContestationLivraison.objects.filter(
        livraison__commande=OuterRef("commande"), statut=ContestationLivraison.Statut.OUVERTE,
    )
    return (
        GainFidelite.objects.filter(statut=Statut.EN_ATTENTE, date_disponibilite__lte=maintenant or timezone.now())
        .exclude(Exists(retour_ouvert))
        .exclude(Exists(contestation_ouverte))
    )


def crediter_gain(gain_id):
    """Crédite un gain échu (sous verrou : une tâche rejouée ne crédite
    jamais deux fois). Renvoie le gain crédité ou None."""
    with transaction.atomic():
        gain = GainFidelite.objects.select_for_update().select_related("commande").filter(
            pk=gain_id, statut=Statut.EN_ATTENTE,
        ).first()
        if gain is None:
            return None
        gain.compte.crediter_points(
            points=gain.points,
            description=f"Points de la commande {gain.commande.numero_commande}",
            reference_externe=gain.commande.numero_commande,
        )
        gain.statut = Statut.CREDITE
        gain.date_traitement = timezone.now()
        gain.save(update_fields=["statut", "date_traitement"])
        transaction.on_commit(lambda: _notifier_credit(gain))
    return gain


def crediter_gains_echus(maintenant=None):
    """Tâche périodique : crédite tous les gains échus. Renvoie le nombre."""
    credites = 0
    for gain_id in gains_echus(maintenant).values_list("pk", flat=True).iterator():
        if crediter_gain(gain_id):
            credites += 1
    return credites


def _notifier_credit(gain):
    from apps.notifications.models import Notification
    from apps.notifications.services import ServiceNotification

    try:
        ServiceNotification.notifier_utilisateur(
            destinataire=gain.compte.utilisateur,
            titre="Points de fidélité crédités",
            message=f"{gain.points} points de la commande {gain.commande.numero_commande} sont maintenant disponibles.",
            type_notification=Notification.TypeNotification.SYSTEME,
            lien_redirection="/fidelite/mon-compte",
            metadata={"points": gain.points, "commande": str(gain.commande_id)},
        )
    except Exception:
        logger.exception("Notification de crédit de points impossible (gain %s).", gain.pk)


def points_en_attente(utilisateur):
    return GainFidelite.objects.filter(compte__utilisateur_id=getattr(utilisateur, "pk", utilisateur), statut=Statut.EN_ATTENTE).aggregate(
        total=Sum("points"),
    )["total"] or 0


# =====================================================================
# COUPONS
# =====================================================================

def consommer_coupon(coupon, client, groupe):
    """Checkout : une utilisation par client ; le coupon (verrouillé par
    l'appelant) est épuisé quand la limite globale est atteinte."""
    UtilisationCoupon.objects.create(coupon=coupon, client=client, groupe=groupe)
    coupon.nombre_utilisations = F("nombre_utilisations") + 1
    coupon.save(update_fields=["nombre_utilisations"])
    coupon.refresh_from_db(fields=["nombre_utilisations"])
    if coupon.utilisations_max is not None and coupon.nombre_utilisations >= coupon.utilisations_max:
        coupon.est_utilise = True
        coupon.save(update_fields=["est_utilise"])


def restituer_coupon(commande):
    """Appelée à l'annulation d'une commande (dans sa transaction) : si toutes
    les commandes du checkout sont annulées et que le coupon n'a pas expiré,
    son utilisation est effacée et le coupon redevient utilisable."""
    from apps.commandes.models import Commande

    if not commande.coupon_code or commande.groupe_id is None:
        return False
    if Commande.objects.filter(groupe_id=commande.groupe_id).exclude(status=Commande.Status.ANNULEE).exists():
        return False
    coupon = CouponReduction.objects.select_for_update().filter(code__iexact=commande.coupon_code).first()
    if coupon is None or (coupon.date_expiration and coupon.date_expiration <= timezone.now()):
        return False
    supprimees, _ = UtilisationCoupon.objects.filter(
        coupon=coupon, client_id=commande.client_id, groupe_id=commande.groupe_id,
    ).delete()
    if not supprimees:
        return False
    coupon.nombre_utilisations = max(coupon.nombre_utilisations - 1, 0)
    coupon.est_utilise = False
    coupon.save(update_fields=["nombre_utilisations", "est_utilise"])
    logger.info("Coupon %s rendu au client_id=%s (commandes annulées).", coupon.code, commande.client_id)
    return True
