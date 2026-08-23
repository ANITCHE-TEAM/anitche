from rest_framework import serializers
from .models import Categorie, Produit, ImageProduit, VarianteProduit, Stock
from apps.vendeurs.serializers import BoutiquePubliqueSerializer


class SousCategorieSerializer(serializers.ModelSerializer):
    class Meta:
        model = Categorie
        fields = ['id', 'nom', 'slug', 'description', 'image', 'est_active', 'ordre']


class CategorieSerializer(serializers.ModelSerializer):
    sous_categories = SousCategorieSerializer(many=True, read_only=True)

    class Meta:
        model = Categorie
        fields = [
            'id',
            'nom',
            'slug',
            'description',
            'image',
            'parent',
            'est_active',
            'ordre',
            'sous_categories',
        ]
        read_only_fields = ['id', 'slug']


class ImageProduitSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImageProduit
        fields = ['id', 'produit', 'image', 'est_principale', 'ordre', 'date_creation']
        read_only_fields = ['id', 'date_creation']


class StockSerializer(serializers.ModelSerializer):
    est_en_stock = serializers.BooleanField(read_only=True)

    class Meta:
        model = Stock
        fields = ['id', 'quantite_disponible', 'seuil_alerte', 'est_en_stock', 'date_mise_a_jour']
        read_only_fields = ['id', 'date_mise_a_jour']


class StockUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Stock
        fields = ['quantite_disponible', 'seuil_alerte']


class VarianteProduitSerializer(serializers.ModelSerializer):
    stock = StockSerializer(read_only=True)
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
        read_only_fields = ['id', 'sku', 'date_creation', 'date_mise_a_jour']


class VarianteVendeurCreateSerializer(serializers.ModelSerializer):
    quantite_initiale = serializers.IntegerField(write_only=True, required=False, default=0, min_value=0)
    seuil_alerte = serializers.IntegerField(write_only=True, required=False, default=5, min_value=0)
    stock = StockSerializer(read_only=True)
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
            'quantite_initiale',
            'seuil_alerte',
            'stock',
        ]
        read_only_fields = ['id', 'sku', 'produit']

    def create(self, validated_data):
        quantite_initiale = validated_data.pop('quantite_initiale', 0)
        seuil_alerte = validated_data.pop('seuil_alerte', 5)
        variante = super().create(validated_data)
        
        stock, _ = Stock.objects.get_or_create(variante=variante)
        stock.quantite_disponible = quantite_initiale
        stock.seuil_alerte = seuil_alerte
        stock.save()
        return variante


class ProduitPublicListSerializer(serializers.ModelSerializer):
    categorie_nom = serializers.CharField(source='categorie.nom', read_only=True)
    boutique_nom = serializers.CharField(source='boutique.nom', read_only=True)
    boutique_slug = serializers.CharField(source='boutique.slug', read_only=True)
    image_principale = serializers.SerializerMethodField()
    prix_min = serializers.SerializerMethodField()
    en_stock = serializers.SerializerMethodField()

    class Meta:
        model = Produit
        fields = [
            'id',
            'nom',
            'slug',
            'prix_base',
            'prix_min',
            'image_principale',
            'categorie',
            'categorie_nom',
            'boutique',
            'boutique_nom',
            'boutique_slug',
            'en_stock',
            'date_creation',
        ]

    def get_image_principale(self, obj):
        image = obj.images.filter(est_principale=True).first() or obj.images.first()
        if image and image.image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(image.image.url)
            return image.image.url
        return None

    def get_prix_min(self, obj):
        variantes = obj.variantes.filter(est_active=True)
        if variantes.exists():
            return min(v.prix_effectif for v in variantes)
        return obj.prix_base

    def get_en_stock(self, obj):
        return any(
            v.stock.quantite_disponible > 0
            for v in obj.variantes.filter(est_active=True).select_related('stock')
            if hasattr(v, 'stock')
        )


class ProduitPublicDetailSerializer(serializers.ModelSerializer):
    categorie = CategorieSerializer(read_only=True)
    boutique = BoutiquePubliqueSerializer(read_only=True)
    images = ImageProduitSerializer(many=True, read_only=True)
    variantes = serializers.SerializerMethodField()

    class Meta:
        model = Produit
        fields = [
            'id',
            'nom',
            'slug',
            'description',
            'prix_base',
            'est_actif',
            'est_achetable',
            'categorie',
            'boutique',
            'images',
            'variantes',
            'date_creation',
            'date_mise_a_jour',
        ]

    def get_variantes(self, obj):
        variantes_actives = obj.variantes.filter(est_active=True).select_related('stock')
        return VarianteProduitSerializer(variantes_actives, many=True, context=self.context).data


class ProduitVendeurSerializer(serializers.ModelSerializer):
    images = ImageProduitSerializer(many=True, read_only=True)
    variantes = VarianteProduitSerializer(many=True, read_only=True)
    boutique_nom = serializers.CharField(source='boutique.nom', read_only=True)

    class Meta:
        model = Produit
        fields = [
            'id',
            'boutique',
            'boutique_nom',
            'categorie',
            'nom',
            'slug',
            'description',
            'prix_base',
            'est_actif',
            'est_achetable',
            'images',
            'variantes',
            'date_creation',
            'date_mise_a_jour',
        ]
        read_only_fields = ['id', 'boutique', 'slug', 'date_creation', 'date_mise_a_jour']
