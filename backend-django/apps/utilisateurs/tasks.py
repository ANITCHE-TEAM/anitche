from celery import shared_task
from django.utils import timezone

from .models import CodeOTP


@shared_task
def nettoyer_otp_expires():
    """
    Tâche asynchrone exécutée par Celery.

    Supprime les codes OTP expirés depuis plus de 24 heures
    afin de limiter la taille de la base de données.
    """

    # Seuil à partir duquel les OTP sont considérés
    # comme suffisamment anciens pour être supprimés.
    seuil = timezone.now() - timezone.timedelta(hours=24)

    # Suppression des OTP expirés avant ce seuil.
    supprimes, _ = CodeOTP.objects.filter(
        date_expiration__lt=seuil
    ).delete()

    # Retourne un résumé utile pour les logs ou le monitoring.
    return f"{supprimes} codes OTP supprimés."