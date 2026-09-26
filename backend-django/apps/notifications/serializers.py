from rest_framework import serializers
from .models import Notification, PreferenceNotification


class NotificationSerializer(serializers.ModelSerializer):
    type_notification_display = serializers.CharField(source="get_type_notification_display", read_only=True)
    canal_display = serializers.CharField(source="get_canal_display", read_only=True)

    class Meta:
        model = Notification
        fields = [
            "id",
            "titre",
            "message",
            "type_notification",
            "type_notification_display",
            "canal",
            "canal_display",
            "est_lu",
            "date_lecture",
            "lien_redirection",
            "metadata",
            "date_creation",
        ]
        read_only_fields = fields


class PreferenceNotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = PreferenceNotification
        fields = [
            "id",
            "email_actif",
            "sms_actif",
            "in_app_actif",
            "date_mise_a_jour",
        ]
        read_only_fields = ["id", "date_mise_a_jour"]
