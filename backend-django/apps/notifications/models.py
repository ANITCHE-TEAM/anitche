import uuid
from django.db import models
from django.conf import settings
from django.utils import timezone


class Notification(models.Model):
    """Notification destinée à un utilisateur (client, vendeur, livreur, administrateur)."""

    class TypeNotification(models.TextChoices):
        COMMANDE = "commande", "Commande"
        PAIEMENT = "paiement", "Paiement"
        LIVRAISON = "livraison", "Livraison"
        KYC = "kyc", "Vérification KYC"
        STOCK = "stock", "Alerte de Stock"
        SUPPORT = "support", "Support Client"
        SYSTEME = "systeme", "Système"

    class Canal(models.TextChoices):
        IN_APP = "in_app", "In-App"
        EMAIL = "email", "Email"
        SMS = "sms", "SMS"
        PUSH = "push", "Push"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    destinataire = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
        verbose_name="Destinataire",
    )

    titre = models.CharField(max_length=200)
    message = models.TextField()

    type_notification = models.CharField(
        max_length=20,
        choices=TypeNotification.choices,
        default=TypeNotification.SYSTEME,
        db_index=True,
    )

    canal = models.CharField(
        max_length=15,
        choices=Canal.choices,
        default=Canal.IN_APP,
    )

    est_lu = models.BooleanField(default=False, db_index=True)
    date_lecture = models.DateTimeField(null=True, blank=True)

    lien_redirection = models.CharField(
        max_length=255,
        blank=True,
        help_text="URL relative ou deep-link vers la ressource associée (ex: /commandes/CMD-2026-X)",
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Données contextuelles additionnelles (IDs techniques, totaux, etc.)",
    )

    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Notification"
        verbose_name_plural = "Notifications"
        ordering = ["-date_creation"]

    def __str__(self):
        return f"[{self.get_type_notification_display()}] {self.titre} -> {self.destinataire.email}"

    def marquer_comme_lue(self):
        """Marque la notification comme lue."""
        if not self.est_lu:
            self.est_lu = True
            self.date_lecture = timezone.now()
            self.save(update_fields=["est_lu", "date_lecture"])

    def marquer_comme_non_lue(self):
        """Marque la notification comme non lue."""
        if self.est_lu:
            self.est_lu = False
            self.date_lecture = None
            self.save(update_fields=["est_lu", "date_lecture"])


class PreferenceNotification(models.Model):
    """Préférences de réception des notifications par canal pour chaque utilisateur."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    utilisateur = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="preferences_notification",
        verbose_name="Utilisateur",
    )

    email_actif = models.BooleanField(default=True, help_text="Recevoir les notifications importantes par email")
    sms_actif = models.BooleanField(default=True, help_text="Recevoir les notifications urgentes et OTP par SMS")
    in_app_actif = models.BooleanField(default=True, help_text="Recevoir les alertes dans le centre de notifications in-app")

    date_mise_a_jour = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Préférence de notification"
        verbose_name_plural = "Préférences de notification"

    def __str__(self):
        return f"Préférences de {self.utilisateur.email}"
