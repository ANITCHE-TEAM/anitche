from celery import shared_task

from .services import expirer_commandes_impayees


@shared_task
def expirer_commandes_non_payees():
    """Annule les commandes non payées dans le délai et restitue leur stock.

    Planifiée toutes les 5 minutes (CELERY_BEAT_SCHEDULE, config/settings/base.py).
    """
    annulees = expirer_commandes_impayees()
    return f"{annulees} commande(s) non payée(s) annulée(s)."
