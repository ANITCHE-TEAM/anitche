from celery import shared_task

from .reversements import rendre_disponibles
from .services import reconcilier_paiements_en_suspens


@shared_task
def rendre_reversements_disponibles():
    """Délai de rétractation écoulé → reversement disponible.

    Planifiée toutes les heures (CELERY_BEAT_SCHEDULE, config/settings/base.py).
    """
    return f"{rendre_disponibles()} reversement(s) disponible(s)."


@shared_task
def reconcilier_paiements():
    """Webhooks perdus : état des paiements redemandé au fournisseur.

    Planifiée toutes les 10 minutes (CELERY_BEAT_SCHEDULE, config/settings/base.py).
    """
    verifies, changes, interrompu = reconcilier_paiements_en_suspens()
    suite = " Interrompue : fournisseur injoignable." if interrompu else ""
    return f"{verifies} paiement(s) vérifié(s), {changes} statut(s) changé(s).{suite}"
