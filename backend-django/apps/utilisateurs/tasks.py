from celery import shared_task
from django.utils import timezone

from .models import CodeOTP


@shared_task
def nettoyer_otp_expires():
    seuil = timezone.now() - timezone.timedelta(hours=24)
    supprimes, _ = CodeOTP.objects.filter(date_expiration__lt=seuil).delete()
    return f"{supprimes} codes OTP supprimés."