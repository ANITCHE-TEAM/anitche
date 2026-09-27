from django.urls import reverse
from rest_framework import serializers

from apps.catalogue.models import Produit
from apps.commandes.models import Commande, CommandeItem

from .models import SupportTicket, TicketAttachment, TicketMessage


# serializers.py
class SupportTicketSerializer(serializers.ModelSerializer):
    """Ticket de support. Le contenu original (subject/description/order/
    product) est immuable après création : le client corrige ou précise via
    un nouveau message (TicketMessage), jamais en réécrivant la réclamation
    initiale. category/priority restent modifiables, mais uniquement par le
    staff (voir SupportRetrieveUpdateView.perform_update).

    `order` : une commande du demandeur ; `product` : un produit qu'il a
    acheté (dans cette commande si elle est donnée). `vendor` est déduit
    côté serveur (voir SupportTicketListCreateView.perform_create)."""

    order = serializers.PrimaryKeyRelatedField(queryset=Commande.objects.all(), required=False, allow_null=True)
    product = serializers.PrimaryKeyRelatedField(queryset=Produit.objects.all(), required=False, allow_null=True)

    class Meta:
        model = SupportTicket
        fields = [
            "id", "ticket_number", "subject", "description",
            "category", "status", "priority",
            "created_by", "assigned_to", "vendor",
            "created_at", "updated_at", "order", "product",
            "satisfaction_rating",
        ]
        read_only_fields = [
            "id", "ticket_number", "created_by",
            "assigned_to", "status", "created_at", "updated_at",
            "satisfaction_rating", "vendor",
        ]

    def _user(self):
        return self.context["request"].user

    def validate_order(self, order):
        # Même message qu'une commande inexistante : l'existence d'une
        # commande d'autrui n'est jamais révélée (IDOR).
        if self.instance is not None:
            return order  # modification : ignorée dans validate()
        if order is not None and order.client_id != self._user().pk:
            raise serializers.ValidationError("Commande introuvable.")
        return order

    def validate(self, attrs):
        if self.instance is not None:
            # Réclamation initiale immuable (seuls category/priority changent).
            for champ in ("subject", "description", "order", "product"):
                attrs.pop(champ, None)
            return attrs
        product = attrs.get("product")
        if product is not None:
            achats = CommandeItem.objects.filter(commande__client=self._user(), variante__produit=product)
            if attrs.get("order") is not None:
                achats = achats.filter(commande=attrs["order"])
            if not achats.exists():
                raise serializers.ValidationError({"product": "Produit introuvable parmi vos achats."})
        return attrs


class TicketMessageSerializer(serializers.ModelSerializer):
    """Message d'un ticket. ticket_link/author/author_role verrouillés :
    tous injectés côté serveur depuis la vue (jamais depuis le payload
    client), ticket_link vient de l'URL, author/author_role de request.user.
    is_internal_note n'est retenu que pour le staff (services.add_message)."""

    class Meta:
        model = TicketMessage
        fields = [
            "id", "ticket_link", "author", "author_role", "content",
            "is_internal_note", "read_at", "created_at"
        ]
        read_only_fields = ["id", "ticket_link", "author", "author_role", "read_at", "created_at"]


class TicketAttachmentSerializer(serializers.ModelSerializer):
    """Pièce jointe. message verrouillé (vient de l'URL, injecté côté
    serveur) ; file_type, file_size et original_filename calculés côté
    serveur depuis le fichier réel. `file` en sortie : l'URL de
    téléchargement authentifiée (jamais le chemin /media/)."""

    class Meta:
        model = TicketAttachment
        fields = [
            "id", "message", "file", "file_type", "original_filename",
            "file_size", "created_at"
        ]
        read_only_fields = ["id", "message", "file_type", "original_filename", "file_size", "created_at"]

    def to_representation(self, instance):
        donnees = super().to_representation(instance)
        chemin = reverse("support:attachment-download", kwargs={"pk": instance.pk})
        requete = self.context.get("request")
        donnees["file"] = requete.build_absolute_uri(chemin) if requete else chemin
        return donnees
