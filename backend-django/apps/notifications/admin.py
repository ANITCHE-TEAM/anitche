from django.contrib import admin
from .models import Notification, PreferenceNotification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = (
        "titre",
        "destinataire",
        "type_notification",
        "canal",
        "est_lu",
        "date_creation",
    )
    list_filter = ("type_notification", "canal", "est_lu", "date_creation")
    search_fields = ("titre", "message", "destinataire__email")
    readonly_fields = ("id", "date_creation", "date_lecture")
    ordering = ("-date_creation",)


@admin.register(PreferenceNotification)
class PreferenceNotificationAdmin(admin.ModelAdmin):
    list_display = (
        "utilisateur",
        "email_actif",
        "sms_actif",
        "in_app_actif",
        "date_mise_a_jour",
    )
    list_filter = ("email_actif", "sms_actif", "in_app_actif")
    search_fields = ("utilisateur__email",)
    readonly_fields = ("id", "date_mise_a_jour")
