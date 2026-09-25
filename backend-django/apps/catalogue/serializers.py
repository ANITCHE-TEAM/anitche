from django.db import transaction
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from .models import Categorie, Produit, ImageProduit, VarianteProduit, Stock
from apps.vendeurs.permissions import ROLES_ADMINISTRATION
from apps.vendeurs.serializers import BoutiquePubliqueSerializer

MESSAGE_MONTANT_ENTIER = (
    "Les montants sont en FCFA et doivent être entiers (les paiements mobile money "
    "ne gèrent pas de centimes)."
)
MESSAGE_DESACTIVATION_ADMINISTRATION = (
    "Ce produit a été désactivé par l'administration ANITCHE : seule l'administration "
    "peut le réactiver."
)


def valider_montant_entier(montant):
    """FCFA : aucun montant fractionnaire (colonnes DecimalField conservées)."""
    if montant is not None and montant != montant.to_integral_value():
        raise serializers.ValidationError(MESSAGE_MONTANT_ENTIER)
    return montant


def origine_desactivation(utilisateur):
    """Qui agit sur l'activation d'un produit : administration ou vendeur."""
    if utilisateur.role in ROLES_ADMINISTRATION:
        return Produit.OrigineDesactivation.ADMINISTRATION
    return Produit.OrigineDesactivation.VENDEUR


class SousCategorieSerializer(serializers.ModelSerializer):
    class Meta:
        model = Categorie
        fields = ['id', 'nom', 'slug', 'description', 'image', 'est_active', 'ordre']


class CategorieSerializer(serializers.ModelSerializer):
    # Les vues publiques préchargent uniquement les sous-catégories actives.
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
    """Taille (5 Mo), format et signature binaire : `validateur_image_standard`
    du modèle, repris automatiquement par ce serializer. Nombre d'images par
    produit : limité dans ImageProduitListCreateView.

    `produit` est volontairement en lecture seule : la vue de création fixe
    déjà le produit, mais le verrouiller aussi ici évite qu'un futur endpoint
    réutilisant ce serializer n'expose un IDOR (attacher une image au produit
    d'un autre vendeur via un champ non protégé).
    """

    class Meta:
        model = ImageProduit
        fields = ['id', 'produit', 'image', 'est_principale', 'ordre', 'date_creation']
        read_only_fields = ['id', 'produit', 'date_creation']


class StockSerializer(serializers.ModelSerializer):
    """Stock complet : réservé au vendeur propriétaire."""

    est_en_stock = serializers.BooleanField(read_only=True)

    class Meta:
        model = Stock
        fields = ['id', 'quantite_disponible', 'seuil_alerte', 'est_en_stock', 'date_mise_a_jour']
        read_only_fields = ['id', 'date_mise_a_jour']


class StockPublicSerializer(serializers.ModelSerializer):
    """Disponibilité seule : ni `quantite_disponible` ni `seuil_alerte`
    (donnée interne du vendeur, exploitable par un concurrent)."""

    est_en_stock = serializers.BooleanField(read_only=True)

    class Meta:
        model = Stock
        fields = ['est_en_stock']
        read_only_fields = fields


class StockUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Stock
        fields = ['quantite_disponible', 'seuil_alerte']

    def update(self, instance, validated_data):
        """Écriture partielle sous verrou : même verrou que le checkout
        (commandes.ValiderPanierView), et seuls les champs envoyés sont
        écrits. Un save() complet réécrivait la quantité lue en début de
        requête et annulait les ventes faites entre-temps."""
        with transaction.atomic():
            stock = Stock.objects.select_for_update().get(pk=instance.pk)
            for champ, valeur in validated_data.items():
                setattr(stock, champ, valeur)
            stock.save(update_fields=[*validated_data, 'date_mise_a_jour'])
        return stock


class ReglesPrixVarianteMixin:
    """Prix en FCFA entiers, prix > 0 (un prix nul permettrait une commande
    gratuite), 0 < prix_promo < prix, poids ≥ 0. Mêmes règles en base
    (CheckConstraint du modèle)."""

    def validate_prix(self, prix):
        valider_montant_entier(prix)
        if prix <= 0:
            raise serializers.ValidationError("Le prix doit être strictement positif.")
        return prix

    def validate_prix_promo(self, prix_promo):
        valider_montant_entier(prix_promo)
        if prix_promo is not None and prix_promo <= 0:
            raise serializers.ValidationError("Le prix promotionnel doit être strictement positif.")
        return prix_promo

    def validate_poids_kg(self, poids_kg):
        if poids_kg is not None and poids_kg < 0:
            raise serializers.ValidationError("Le poids ne peut pas être négatif.")
        return poids_kg

    def valider_promo_inferieure_au_prix(self, attrs):
        instance = getattr(self, 'instance', None)
        prix = attrs.get('prix', getattr(instance, 'prix', None))
        prix_promo = attrs['prix_promo'] if 'prix_promo' in attrs else getattr(instance, 'prix_promo', None)
        if prix_promo is not None and prix is not None and prix_promo >= prix:
            raise serializers.ValidationError(
                {'prix_promo': "Le prix promotionnel doit être inférieur au prix."}
            )


class VarianteProduitSerializer(ReglesPrixVarianteMixin, serializers.ModelSerializer):
    """Variante vue et modifiée par son vendeur (stock complet)."""

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
        # `produit` modifiable permettait de rattacher sa variante au produit
        # d'un autre vendeur (et de la faire apparaître sur SA fiche).
        read_only_fields = ['id', 'produit', 'sku', 'date_creation', 'date_mise_a_jour']

    def validate(self, attrs):
        # Refus explicite plutôt qu'ignoré : un client qui croirait déplacer
        # la variante doit le savoir. Renvoyer le même produit (PUT) reste accepté.
        if self.instance is not None and 'produit' in self.initial_data:
            if str(self.initial_data.get('produit')) != str(self.instance.produit_id):
                raise serializers.ValidationError(
                    {'produit': "Une variante ne peut pas changer de produit. Créez-la sur le bon produit."}
                )
        self.valider_promo_inferieure_au_prix(attrs)
        return attrs


class VariantePubliqueSerializer(serializers.ModelSerializer):
    """Variante sur la fiche publique : disponibilité seule côté stock."""

    stock = StockPublicSerializer(read_only=True)
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


class VarianteVendeurCreateSerializer(ReglesPrixVarianteMixin, serializers.ModelSerializer):
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

    def validate(self, attrs):
        self.valider_promo_inferieure_au_prix(attrs)
        return attrs

    def create(self, validated_data):
        quantite_initiale = validated_data.pop('quantite_initiale', 0)
        seuil_alerte = validated_data.pop('seuil_alerte', 5)
        variante = super().create(validated_data)

        stock, _ = Stock.objects.get_or_create(variante=variante)
        stock.quantite_disponible = quantite_initiale
        stock.seuil_alerte = seuil_alerte
        stock.save()
        # Sans cette ligne, la réponse API peut sérialiser une version en
        # cache de `variante.stock` antérieure à la mise à jour ci-dessus
        # (quantite_disponible=0) même si la valeur réellement persistée en
        # base est correcte — la réponse mentirait au vendeur sur son
        # propre stock qu'il vient de créer.
        variante.stock = stock
        return variante


class ProduitPublicListSerializer(serializers.ModelSerializer):
    """Vignette de liste. Tout vient du queryset de ProduitPublicListView
    (images préchargées, annotations `prix_min_effectif` et `a_du_stock`) :
    aucune requête par produit."""

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
        images = list(obj.images.all())
        image = next((i for i in images if i.est_principale), images[0] if images else None)
        if image and image.image:
            request = self.context.get('request')
            if request:
                return request.build_absolute_uri(image.image.url)
            return image.image.url
        return None

    def get_prix_min(self, obj):
        """Plus petit prix effectif (promo comprise) des variantes actives."""
        if obj.prix_min_effectif is not None:
            return obj.prix_min_effectif
        return obj.prix_base

    def get_en_stock(self, obj):
        return obj.a_du_stock


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
        variantes_actives = getattr(obj, 'variantes_actives', None)
        if variantes_actives is None:
            variantes_actives = obj.variantes.filter(est_active=True).select_related('stock')
        return VariantePubliqueSerializer(variantes_actives, many=True, context=self.context).data


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
            'desactive_par',
            'est_achetable',
            'images',
            'variantes',
            'date_creation',
            'date_mise_a_jour',
        ]
        read_only_fields = ['id', 'boutique', 'slug', 'desactive_par', 'date_creation', 'date_mise_a_jour']

    def validate_prix_base(self, prix_base):
        valider_montant_entier(prix_base)
        if prix_base < 0:
            raise serializers.ValidationError("Le prix de base ne peut pas être négatif.")
        return prix_base

    def create(self, validated_data):
        if not validated_data.get('est_actif', True):
            validated_data['desactive_par'] = Produit.OrigineDesactivation.VENDEUR
        return super().create(validated_data)

    def update(self, instance, validated_data):
        """N'écrit que les champs envoyés : un save() complet réécrirait
        `est_actif` lu en début de requête et annulerait une désactivation
        faite entre-temps par l'administration. L'activation passe par
        desactiver() / reactiver(), qui enregistrent qui a désactivé.
        Tout ou rien : une réactivation refusée n'applique aucun autre champ.
        """
        est_actif = validated_data.pop('est_actif', None)
        origine = origine_desactivation(self.context['request'].user)
        with transaction.atomic():
            if validated_data:
                for champ, valeur in validated_data.items():
                    setattr(instance, champ, valeur)
                instance.save(update_fields=[*validated_data, 'date_mise_a_jour'])
            if est_actif is False:
                instance.desactiver(par=origine)
            elif est_actif is True and not instance.reactiver(par=origine):
                raise PermissionDenied(MESSAGE_DESACTIVATION_ADMINISTRATION)
        return instance


class ProduitAdministrationSerializer(serializers.ModelSerializer):
    """Modération : l'administration ne décide que de l'activation
    (`est_actif`). Le contenu appartient au vendeur : tout autre champ envoyé
    est refusé (400) plutôt qu'ignoré — même règle que `est_suspendue` sur
    BoutiqueAdministrationSerializer."""

    CHAMPS_MODIFIABLES = {'est_actif'}

    boutique_nom = serializers.CharField(source='boutique.nom', read_only=True)

    class Meta:
        model = Produit
        fields = [
            'id', 'boutique', 'boutique_nom', 'nom', 'slug', 'est_actif', 'desactive_par',
            'date_creation', 'date_mise_a_jour',
        ]
        read_only_fields = [champ for champ in fields if champ != 'est_actif']

    def validate(self, attrs):
        champs_refuses = sorted(set(self.initial_data) - self.CHAMPS_MODIFIABLES)
        if champs_refuses:
            raise serializers.ValidationError(
                "Seul le champ est_actif est modifiable ici "
                f"(champs refusés : {', '.join(champs_refuses)})."
            )
        return attrs

    def update(self, instance, validated_data):
        administration = Produit.OrigineDesactivation.ADMINISTRATION
        if validated_data.get('est_actif') is False:
            instance.desactiver(par=administration)
        elif validated_data.get('est_actif') is True:
            instance.reactiver(par=administration)
        return instance
