from celery import shared_task

from .reversements import rendre_disponibles


@shared_task
def rendre_reversements_disponibles():
    """Délai de rétractation écoulé → reversement disponible.

    Planifiée toutes les heures (CELERY_BEAT_SCHEDULE, config/settings/base.py).
    """
    return f"{rendre_disponibles()} reversement(s) disponible(s)."
