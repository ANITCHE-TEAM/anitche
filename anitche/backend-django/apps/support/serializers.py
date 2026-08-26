from rest_framework import serializers
from .models import SupportTicket, TicketMessage, TicketAttachment


class SupportTicketSerializer(serializers.ModelSerializer):
    """Ticket de support. status modifiable seulement via l'action dédiée,
    pas via ce serializer."""

    class Meta:
        model = SupportTicket
        fields = [
            "id", "ticket_number", "subject", "description",
            "category", "status", "priority",
            "created_by", "assigned_to", "vendor",
            "created_at", "updated_at", "product",
            "satisfaction_rating",
        ]
        read_only_fields = [
            "id", "ticket_number", "created_by",
            "assigned_to", "status", "created_at", "updated_at",
            "satisfaction_rating",
        ]


class TicketMessageSerializer(serializers.ModelSerializer):
    """Message d'un ticket. ticket_link/author/author_role verrouillés :
    tous injectés côté serveur depuis la vue (jamais depuis le payload
    client), ticket_link vient de l'URL, author/author_role de request.user."""

    class Meta:
        model = TicketMessage
        fields = [
            "id", "ticket_link", "author", "author_role", "content",
            "read_at", "created_at"
        ]
        read_only_fields = ["id", "ticket_link", "author", "author_role", "read_at", "created_at"]



class TicketAttachmentSerializer(serializers.ModelSerializer):
    """Pièce jointe. message verrouillé (vient de l'URL, injecté côté
    serveur) ; file_size/original_filename recalculés côté serveur
    depuis le fichier uploadé réel, même si le client en envoie d'autres."""

    class Meta:
        model = TicketAttachment
        fields = [
            "id", "message", "file", "file_type", "original_filename",
            "file_size", "created_at"
        ]
        read_only_fields = ["id", "message", "original_filename", "file_size", "created_at"]