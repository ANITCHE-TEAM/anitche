from celery import shared_task

from .services import appliquer_echeances


@shared_task
def appliquer_echeances_retours():
    """Demandes sans réponse du vendeur → modération ; refus non contestés
    → définitifs ; rappels au vendeur et alertes de délai.

    Planifiée toutes les 15 minutes (CELERY_BEAT_SCHEDULE, config/settings/base.py).
    """
    escaladees, closes, relances = appliquer_echeances()
    return f"{escaladees} demande(s) escaladée(s), {closes} refus clos, {relances} relance(s)."
