from rest_framework import serializers
from .models import Panier, PanierItem
from apps.catalogue.serializers import VarianteProduitSerializer


class PanierItemSerializer(serializers.ModelSerializer):
    variante_detail = VarianteProduitSerializer(source='variante', read_only=True)
    prix_unitaire = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    sous_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = PanierItem
        fields = [
            "id",
            "panier",
            "variante",
            "variante_detail",
            "quantite",
            "prix_unitaire",
            "sous_total",
            "added_at",
        ]
        read_only_fields = [
            "id",
            "added_at",
            "panier",
            "prix_unitaire",
            "sous_total",
        ]


class PanierSerializer(serializers.ModelSerializer):
    items = PanierItemSerializer(many=True, read_only=True)
    total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    nombre_articles = serializers.IntegerField(read_only=True)

    class Meta:
        model = Panier
        fields = [
            "id",
            "utilisateur",
            "session_key",
            "items",
            "total",
            "nombre_articles",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "utilisateur",
            "session_key",
            "created_at",
            "updated_at",
            "total",
            "nombre_articles",
        ]