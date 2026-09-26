from celery import shared_task

from .services import crediter_gains_echus


@shared_task
def crediter_points_echus():
    """Délai de rétractation écoulé → points en attente crédités.

    Planifiée toutes les heures, juste après la mise à disposition des
    reversements (CELERY_BEAT_SCHEDULE, config/settings/base.py).
    """
    return f"{crediter_gains_echus()} gain(s) de fidélité crédité(s)."
