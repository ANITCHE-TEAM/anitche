from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from .models import PasseportProduit, HistoriqueScanPasseport
from apps.catalogue.models import Produit, VarianteProduit


class PasseportPublicSerializer(serializers.ModelSerializer):
    produit_nom = serializers.CharField(source="produit.nom", read_only=True)
    produit_slug = serializers.CharField(source="produit.slug", read_only=True)
    boutique_nom = serializers.CharField(source="boutique.nom", read_only=True)
    variante_nom = serializers.SerializerMethodField()
    statut_certification_display = serializers.CharField(source="get_statut_certification_display", read_only=True)

    class Meta:
        model = PasseportProduit
        fields = [
            "code_passeport",
            "produit_nom",
            "produit_slug",
            "boutique_nom",
            "variante_nom",
            "numero_lot",
            "origine_geographique",
            "materiaux_utilises",
            "date_fabrication",
            "artisan_createur",
            "statut_certification",
            "statut_certification_display",
            "nb_scans",
            "dernier_scan",
            "url_verification_publique",
        ]
        read_only_fields = fields

    def get_variante_nom(self, obj):
        return obj.variante.nom if obj.variante else None


class PasseportVendeurSerializer(serializers.ModelSerializer):
    produit_nom = serializers.CharField(source="produit.nom", read_only=True)
    statut_certification_display = serializers.CharField(source="get_statut_certification_display", read_only=True)

    class Meta:
        model = PasseportProduit
        fields = [
            "id",
            "code_passeport",
            "produit",
            "produit_nom",
            "variante",
            "boutique",
            "numero_lot",
            "origine_geographique",
            "materiaux_utilises",
            "date_fabrication",
            "artisan_createur",
            "statut_certification",
            "statut_certification_display",
            "nb_scans",
            "dernier_scan",
            "url_verification_publique",
            "est_actif",
            "date_creation",
        ]
        read_only_fields = ["id", "code_passeport", "boutique", "nb_scans", "dernier_scan", "url_verification_publique", "date_creation"]

    def validate(self, attrs):
        """F-21 (audit sécurité) : `produit` et `variante` restent modifiables
        (un vendeur peut corriger une erreur de saisie), mais sans validation
        d'appartenance ici, un vendeur pouvait réassigner un passeport
        existant — dont `boutique` reste le sien et donc affiché comme
        authentique — à un `produit_id` appartenant à N'IMPORTE QUEL AUTRE
        vendeur (PATCH direct, en contournant CreerPasseportSerializer.validate
        qui ne s'applique qu'à la création). Ça cassait la garantie même que
        ce module est censé apporter : un passeport de traçabilité pourrait
        certifier un produit qui n'est pas celui du vendeur affiché.
        On revalide donc ici la même règle d'appartenance qu'à la création,
        et on s'assure que la variante appartient bien au produit choisi.
        """
        produit = attrs.get("produit", getattr(self.instance, "produit", None))
        variante = attrs.get("variante", getattr(self.instance, "variante", None))

        boutique_cible = self.instance.boutique if self.instance else None
        if produit is not None and boutique_cible is not None and produit.boutique_id != boutique_cible.id:
            raise serializers.ValidationError(
                {"produit": "Ce produit n'appartient pas à la boutique de ce passeport."}
            )

        if variante is not None and produit is not None and variante.produit_id != produit.id:
            raise serializers.ValidationError(
                {"variante": "Cette variante n'appartient pas à ce produit."}
            )

        return attrs


class CreerPasseportSerializer(serializers.Serializer):
    produit_id = serializers.IntegerField()
    variante_id = serializers.IntegerField(required=False, allow_null=True)
    numero_lot = serializers.CharField(required=False, allow_blank=True, max_length=60, default="")
    origine_geographique = serializers.CharField(required=False, max_length=150, default="Côte d'Ivoire")
    materiaux_utilises = serializers.CharField(required=False, allow_blank=True, default="")
    date_fabrication = serializers.DateField(required=False, allow_null=True)
    artisan_createur = serializers.CharField(required=False, allow_blank=True, max_length=150, default="")
    statut_certification = serializers.ChoiceField(
        choices=PasseportProduit.StatutCertification.choices,
        default=PasseportProduit.StatutCertification.CERTIFIE_AUTHENTIQUE,
    )

    def validate(self, attrs):
        user = self.context["request"].user
        produit_id = attrs["produit_id"]

        try:
            produit = Produit.objects.get(id=produit_id)
        except Produit.DoesNotExist:
            raise ValidationError({"produit_id": "Produit introuvable."})

        # Vérification d'appartenance de la boutique au vendeur connecté
        if getattr(produit.boutique, "proprietaire_id", None) != user.id and not (user.is_staff or user.role == "admin"):
            raise ValidationError({"produit_id": "Ce produit n'appartient pas à votre boutique."})

        variante = None
        variante_id = attrs.get("variante_id")
        if variante_id:
            try:
                variante = VarianteProduit.objects.get(id=variante_id, produit=produit)
            except VarianteProduit.DoesNotExist:
                raise ValidationError({"variante_id": "Cette variante n'appartient pas à ce produit."})

        attrs["_produit"] = produit
        attrs["_variante"] = variante
        attrs["_boutique"] = produit.boutique
        return attrs