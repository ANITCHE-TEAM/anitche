from rest_framework import serializers
from .models import Panier, PanierItem
from apps.catalogue.models import Stock, VarianteProduit


class StockPanierSerializer(serializers.ModelSerializer):
    """Disponibilité seule : ni `quantite_disponible` ni `seuil_alerte`
    (donnée interne du vendeur) ne sont exposés dans le panier."""

    est_en_stock = serializers.BooleanField(read_only=True)

    class Meta:
        model = Stock
        fields = ['est_en_stock']
        read_only_fields = fields


class VariantePanierSerializer(serializers.ModelSerializer):
    """Variante telle qu'affichée dans une ligne de panier (lecture seule)."""

    stock = StockPanierSerializer(read_only=True)
    prix_effectif = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = VarianteProduit
        fields = [
            'id',
            'produit',
            'sku',
            'nom',
            'prix',
            'prix_promo',
            'prix_effectif',
            'poids_kg',
            'est_active',
            'stock',
            'date_creation',
            'date_mise_a_jour',
        ]
        read_only_fields = fields


class PanierItemSerializer(serializers.ModelSerializer):
    variante = serializers.PrimaryKeyRelatedField(
        queryset=VarianteProduit.objects.select_related(
            'stock', 'produit__boutique__proprietaire'
        ),
    )
    variante_detail = VariantePanierSerializer(source='variante', read_only=True)
    prix_unitaire = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    sous_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    est_disponible = serializers.BooleanField(read_only=True)
    motif_indisponibilite = serializers.CharField(read_only=True, allow_null=True)

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
            "est_disponible",
            "motif_indisponibilite",
            "added_at",
        ]
        read_only_fields = [
            "id",
            "added_at",
            "panier",
            "prix_unitaire",
            "sous_total",
        ]
        # Une ligne à 0 n'a pas de sens : pour retirer un article, DELETE.
        extra_kwargs = {"quantite": {"min_value": 1}}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance is not None and not isinstance(self.instance, (list, tuple)):
            # Mise à jour d'une ligne : la variante n'est plus modifiable.
            # Changer de variante contournait les contrôles de stock et de
            # disponibilité (faits sur l'ancienne variante) : il faut
            # supprimer la ligne et ajouter la nouvelle variante.
            self.fields['variante'].required = False

    def validate(self, data):
        if self.instance is not None and 'variante' in data and data['variante'] != self.instance.variante:
            raise serializers.ValidationError({
                "variante": [
                    "La variante d'une ligne de panier ne peut pas être modifiée. "
                    "Supprimez la ligne puis ajoutez la nouvelle variante."
                ]
            })
        return data


class PanierSerializer(serializers.ModelSerializer):
    items = PanierItemSerializer(source='lignes', many=True, read_only=True)
    total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    nombre_articles = serializers.IntegerField(read_only=True)

    class Meta:
        model = Panier
        # `session_key` n'est volontairement pas exposé : c'est la valeur
        # même du cookie de session, que le JSON rendrait lisible par tout
        # script de la page (contournement de HttpOnly en cas de XSS).
        fields = [
            "id",
            "utilisateur",
            "items",
            "total",
            "nombre_articles",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "utilisateur",
            "created_at",
            "updated_at",
            "total",
            "nombre_articles",
        ]
