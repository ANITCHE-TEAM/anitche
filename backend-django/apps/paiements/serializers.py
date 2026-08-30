from decimal import Decimal
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from .models import Paiement, JournalWebhook
from apps.commandes.models import Commande, GroupeCommande


class InitierPaiementSerializer(serializers.Serializer):
    commande_id = serializers.UUIDField(required=False, allow_null=True)
    groupe_commande_id = serializers.UUIDField(required=False, allow_null=True)
    methode = serializers.ChoiceField(
        choices=Paiement.Methode.choices,
        default=Paiement.Methode.WAVE,
    )
    telephone = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=25,
        help_text="Numéro de téléphone pour Mobile Money (Wave, Orange, MTN, Moov)",
    )
    adresse_livraison = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
        help_text="Adresse de livraison des colis",
    )

    def validate(self, attrs):
        commande_id = attrs.get("commande_id")
        groupe_id = attrs.get("groupe_commande_id")

        if not commande_id and not groupe_id:
            raise ValidationError("Vous devez spécifier soit 'commande_id' soit 'groupe_commande_id'.")

        if commande_id and groupe_id:
            raise ValidationError("Veuillez spécifier soit une commande unique, soit un groupe de commandes, pas les deux.")

        user = self.context["request"].user

        if commande_id:
            try:
                commande = Commande.objects.get(id=commande_id, client=user)
            except Commande.DoesNotExist:
                raise ValidationError({"commande_id": "Commande introuvable ou non autorisée."})

            if commande.status in (Commande.Status.CONFIRMEE, Commande.Status.PREPARATION, Commande.Status.EXPEDIEE, Commande.Status.LIVREE):
                raise ValidationError("Cette commande a déjà été payée ou confirmée.")

            if commande.status == Commande.Status.ANNULEE:
                raise ValidationError("Impossible de payer une commande annulée.")

            attrs["_cible_objet"] = commande
            attrs["_type_cible"] = "commande"
            attrs["_montant"] = commande.montant_total

        if groupe_id:
            try:
                groupe = GroupeCommande.objects.get(id=groupe_id, client=user)
            except GroupeCommande.DoesNotExist:
                raise ValidationError({"groupe_commande_id": "Groupe de commandes introuvable ou non autorisé."})

            commandes = list(groupe.commandes.all())
            if not commandes:
                raise ValidationError("Ce groupe de commandes ne contient aucune commande.")

            deja_payee = any(c.status != Commande.Status.CREEE for c in commandes)
            if deja_payee:
                raise ValidationError("Une ou plusieurs commandes de ce groupe ont déjà été validées ou payées.")

            montant_total = sum((c.montant_total for c in commandes), Decimal("0.00"))
            attrs["_cible_objet"] = groupe
            attrs["_type_cible"] = "groupe"
            attrs["_montant"] = montant_total

        return attrs


class PaiementSerializer(serializers.ModelSerializer):
    client_email = serializers.EmailField(source="client.email", read_only=True)
    methode_display = serializers.CharField(source="get_methode_display", read_only=True)
    statut_display = serializers.CharField(source="get_statut_display", read_only=True)

    class Meta:
        model = Paiement
        fields = [
            "id",
            "reference",
            "client",
            "client_email",
            "commande",
            "groupe_commande",
            "methode",
            "methode_display",
            "statut",
            "statut_display",
            "montant",
            "devise",
            "transaction_id_externe",
            "url_paiement",
            "adresse_livraison",
            "metadata",
            "date_creation",
            "date_validation",
            "date_mise_a_jour",
        ]
        read_only_fields = fields


class WebhookPaiementSerializer(serializers.Serializer):
    fournisseur = serializers.CharField(max_length=50)
    evenement_id = serializers.CharField(max_length=150)
    reference = serializers.CharField(max_length=50)
    statut = serializers.ChoiceField(choices=["succes", "echec", "annule"])
    transaction_id_externe = serializers.CharField(max_length=150, required=False, allow_blank=True)
    metadata = serializers.DictField(required=False, default=dict)
