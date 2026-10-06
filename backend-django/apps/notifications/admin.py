from django.contrib import admin

from .models import Notification, PreferenceNotification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    """Consultation seule : réécrire le titre ou le message d'une notification
    déjà reçue ferait mentir l'historique du destinataire."""

    list_display = ("titre", "destinataire", "type_notification", "est_lu", "date_creation")
    list_filter = ("type_notification", "est_lu", "date_creation")
    search_fields = ("titre", "destinataire__email")
    ordering = ("-date_creation",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(PreferenceNotification)
class PreferenceNotificationAdmin(admin.ModelAdmin):
    list_display = ("utilisateur", "email_actif", "date_mise_a_jour")
    list_filter = ("email_actif",)
    search_fields = ("utilisateur__email",)
    readonly_fields = ("id", "date_mise_a_jour")
