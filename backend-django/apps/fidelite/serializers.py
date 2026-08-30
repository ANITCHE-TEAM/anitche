import uuid
from decimal import Decimal
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from .models import CompteFidelite, TransactionFidelite, CouponReduction


class TransactionFideliteSerializer(serializers.ModelSerializer):
    type_transaction_display = serializers.CharField(source="get_type_transaction_display", read_only=True)

    class Meta:
        model = TransactionFidelite
        fields = [
            "id",
            "type_transaction",
            "type_transaction_display",
            "points",
            "solde_apres",
            "description",
            "reference_externe",
            "date_creation",
        ]
        read_only_fields = fields


class CompteFideliteSerializer(serializers.ModelSerializer):
    palier_display = serializers.CharField(source="get_palier_display", read_only=True)
    utilisateur_email = serializers.EmailField(source="utilisateur.email", read_only=True)

    class Meta:
        model = CompteFidelite
        fields = [
            "id",
            "utilisateur_email",
            "solde_points",
            "points_cumules_total",
            "palier",
            "palier_display",
            "date_creation",
            "date_mise_a_jour",
        ]
        read_only_fields = fields


class CouponReductionSerializer(serializers.ModelSerializer):
    type_reduction_display = serializers.CharField(source="get_type_reduction_display", read_only=True)

    class Meta:
        model = CouponReduction
        fields = [
            "id",
            "code",
            "type_reduction",
            "type_reduction_display",
            "valeur",
            "montant_minimum_commande",
            "points_requis",
            "est_actif",
            "est_utilise",
            "date_expiration",
            "date_creation",
        ]
        read_only_fields = fields


class ConvertirPointsCouponSerializer(serializers.Serializer):
    """Options de conversion prédéfinies pour échanger des points contre des bons."""

    OPTIONS_CONVERSION = {
        "50_PTS_5PCT": {"points": 50, "type": "pourcentage", "valeur": Decimal("5.00"), "min": Decimal("5000.00")},
        "100_PTS_10PCT": {"points": 100, "type": "pourcentage", "valeur": Decimal("10.00"), "min": Decimal("10000.00")},
        "250_PTS_2500FCFA": {"points": 250, "type": "montant_fixe", "valeur": Decimal("2500.00"), "min": Decimal("15000.00")},
        "500_PTS_6000FCFA": {"points": 500, "type": "montant_fixe", "valeur": Decimal("6000.00"), "min": Decimal("25000.00")},
    }

    option = serializers.ChoiceField(choices=list(OPTIONS_CONVERSION.keys()))

    def validate(self, attrs):
        user = self.context["request"].user
        compte, _ = CompteFidelite.objects.get_or_create(utilisateur=user)

        option_choisie = attrs["option"]
        regle = self.OPTIONS_CONVERSION[option_choisie]
        points_requis = regle["points"]

        if compte.solde_points < points_requis:
            raise ValidationError(
                f"Solde insuffisant : vous disposez de {compte.solde_points} points, "
                f"mais {points_requis} points sont requis pour cette réduction."
            )

        attrs["_compte"] = compte
        attrs["_regle"] = regle
        return attrs


class VerifierCouponSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=30)
    montant_commande = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.00"))
