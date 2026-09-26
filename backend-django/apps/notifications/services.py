import logging
from django.utils import timezone
from django.core.mail import send_mail
from django.conf import settings

from .models import Notification, PreferenceNotification

logger = logging.getLogger(__name__)


class ServiceNotification:
    """Service centralisé de distribution des notifications."""

    @staticmethod
    def get_preferences(utilisateur):
        """Récupère ou initialise les préférences d'un utilisateur."""
        prefs, _ = PreferenceNotification.objects.get_or_create(utilisateur=utilisateur)
        return prefs

    @classmethod
    def notifier_utilisateur(cls, destinataire, titre, message, type_notification=Notification.TypeNotification.SYSTEME, lien_redirection="", metadata=None):
        """Envoie une notification à l'utilisateur selon ses préférences actives."""
        metadata = metadata or {}
        prefs = cls.get_preferences(destinataire)
        notifications_creees = []

        # 1. Notification In-App
        if prefs.in_app_actif:
            notif_in_app = Notification.objects.create(
                destinataire=destinataire,
                titre=titre,
                message=message,
                type_notification=type_notification,
                canal=Notification.Canal.IN_APP,
                lien_redirection=lien_redirection,
                metadata=metadata,
            )
            notifications_creees.append(notif_in_app)
            logger.info(f"Notification In-App créée pour {destinataire.email} : {titre}")

        # 2. Email transactionnel
        if prefs.email_actif and destinataire.email:
            try:
                # Utilise send_mail de Django configuré avec settings.EMAIL_BACKEND
                send_mail(
                    subject=f"[ANITCHE] {titre}",
                    message=message,
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "no-reply@anitche.ci"),
                    recipient_list=[destinataire.email],
                    fail_silently=True,
                )
            except Exception as e:
                logger.warning(f"Impossible d'envoyer l'email de notification à {destinataire.email}: {e}")

        # 3. SMS (pour les livraisons et paiements urgents)
        if prefs.sms_actif and getattr(destinataire, "telephone", None):
            logger.info(f"SMS simulé pour {destinataire.telephone} : {message[:100]}...")

        return notifications_creees

    @staticmethod
    def marquer_toutes_lues(destinataire):
        """Marque toutes les notifications non lues d'un utilisateur comme lues."""
        now = timezone.now()
        nb_modifiees = Notification.objects.filter(
            destinataire=destinataire,
            est_lu=False,
        ).update(
            est_lu=True,
            date_lecture=now,
        )
        return nb_modifiees
