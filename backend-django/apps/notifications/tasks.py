import logging
import smtplib

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail

from .models import Notification

logger = logging.getLogger(__name__)


@shared_task(autoretry_for=(smtplib.SMTPException, OSError), retry_backoff=30, max_retries=3)
def envoyer_email_notification(notification_id):
    """Email d'une notification, lancé après le commit.

    Seul l'identifiant passe par Redis : la notification est relue ici. Une
    panne SMTP est réessayée (3 fois, délai croissant) puis journalisée, sans
    jamais toucher l'action qui l'a déclenchée (déjà enregistrée).
    """
    notification = Notification.objects.select_related("destinataire").filter(pk=notification_id).first()
    if notification is None or not notification.destinataire.email:
        return "ignorée"
    send_mail(
        subject=f"[ANITCHE] {notification.titre}",
        message=notification.message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[notification.destinataire.email],
        fail_silently=False,
    )
    logger.info("Email de la notification %s envoyé.", notification_id)
    return "envoyée"
