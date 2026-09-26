import logging

from django.db import transaction
from django.db.models import Exists, Min, OuterRef, Prefetch, Q
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle

from apps.vendeurs.permissions import BoutiqueDuVendeurNonSuspendue, EstAdministrateur, EstVendeurValide
from .models import MAX_IMAGES_PAR_PRODUIT, Categorie, Produit, VarianteProduit, ImageProduit, Stock
from .permissions import (
    EstProprietaireDuProduit,
    EstProprietaireDeLaVariante,
    EstProprietaireDeLImage,
)
from .serializers import (
    CategorieSerializer,
    ProduitAdministrationSerializer,
    ProduitPublicListSerializer,
    ProduitPublicDetailSerializer,
    ProduitVendeurSerializer,
    VarianteProduitSerializer,
    VarianteVendeurCreateSerializer,
    ImageProduitSerializer,
    StockUpdateSerializer,
)

logger_securite = logging.getLogger('securite')

#: Espace vendeur : vendeur validé, et boutique non suspendue pour écrire
#: (mêmes règles que ma-boutique/ et les passeports).
PERMISSIONS_VENDEUR = [IsAuthenticated, EstVendeurValide, BoutiqueDuVendeurNonSuspendue]


def sous_categories_actives():
    return Prefetch('sous_categories', queryset=Categorie.objects.actives())


# =====================================================================
# VUES PUBLIQUES (CLIENTS / VISITEURS)
# =====================================================================

class VuePubliqueCatalogueMixin:
    """Limite dédiée, par IP pour un visiteur : parcourir le catalogue
    enchaîne beaucoup de requêtes, et derrière le CGNAT des opérateurs
    mobiles de nombreux clients partagent une IP publique. Le taux 'anon'
    global (partagé avec toute l'API) serait épuisé en quelques minutes."""

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'catalogue_public'


class CategorieListView(VuePubliqueCatalogueMixin, generics.ListAPIView):
    """Liste de toutes les catégories principales et leurs sous-catégories actives."""
    serializer_class = CategorieSerializer
    pagination_class = None

    def get_queryset(self):
        return Categorie.objects.actives().racines().prefetch_related(sous_categories_actives())


class CategorieDetailView(VuePubliqueCatalogueMixin, generics.RetrieveAPIView):
    """Détail d'une catégorie identifiée par son slug."""
    serializer_class = CategorieSerializer
    lookup_field = 'slug'

    def get_queryset(self):
        return Categorie.objects.actives().prefetch_related(sous_categories_actives())


class ProduitPublicListView(VuePubliqueCatalogueMixin, generics.ListAPIView):
    """Recherche et filtrage des produits visibles sur la marketplace."""
    serializer_class = ProduitPublicListSerializer

    def get_queryset(self):
        # Prix affiché, filtré et trié = plus petit prix effectif (promo
        # comprise) des variantes ACTIVES. Les contraintes du modèle
        # garantissent 0 < prix_promo < prix, donc Coalesce suffit.
        queryset = Produit.objects.visibles_publiquement().select_related(
            'boutique', 'categorie'
        ).prefetch_related('images').annotate(
            prix_min_effectif=Min(
                Coalesce('variantes__prix_promo', 'variantes__prix'),
                filter=Q(variantes__est_active=True),
            ),
            a_du_stock=Exists(Stock.objects.filter(
                variante__produit=OuterRef('pk'), variante__est_active=True, quantite_disponible__gt=0,
            )),
        )

        params = self.request.query_params

        # Filtrage par recherche texte
        recherche = params.get('recherche', '')[:100]
        if recherche:
            queryset = queryset.filter(
                Q(nom__icontains=recherche) |
                Q(description__icontains=recherche) |
                Q(boutique__nom__icontains=recherche)
            )

        # Filtrage par catégorie : slug, ou id si la valeur est numérique.
        # Un slug peut aussi être numérique (catégorie « 2024 ») : dans ce
        # cas les deux interprétations sont acceptées.
        categorie = params.get('categorie')
        if categorie:
            filtre_categorie = Q(categorie__slug=categorie) | Q(categorie__parent__slug=categorie)
            if categorie.isdigit():
                filtre_categorie |= Q(categorie_id=categorie) | Q(categorie__parent_id=categorie)
            queryset = queryset.filter(filtre_categorie)

        # Filtrage par boutique (slug ou id)
        boutique = params.get('boutique')
        if boutique:
            if boutique.isdigit():
                queryset = queryset.filter(boutique_id=boutique)
            else:
                queryset = queryset.filter(boutique__slug=boutique)

        # Fourchette de prix (FCFA entiers) sur le prix affiché
        prix_min = params.get('prix_min')
        if prix_min and prix_min.isdigit():
            queryset = queryset.filter(prix_min_effectif__gte=prix_min)

        prix_max = params.get('prix_max')
        if prix_max and prix_max.isdigit():
            queryset = queryset.filter(prix_min_effectif__lte=prix_max)

        # Tri (date en second critère : pagination stable à prix égal)
        tri = params.get('tri')
        if tri == 'prix_asc':
            queryset = queryset.order_by('prix_min_effectif', '-date_creation')
        elif tri == 'prix_desc':
            queryset = queryset.order_by('-prix_min_effectif', '-date_creation')
        elif tri == 'date_asc':
            queryset = queryset.order_by('date_creation')
        else:
            queryset = queryset.order_by('-date_creation')

        return queryset


class ProduitPublicDetailView(VuePubliqueCatalogueMixin, generics.RetrieveAPIView):
    """Fiche produit détaillée (variantes actives, disponibilité, images, boutique)."""
    serializer_class = ProduitPublicDetailSerializer
    lookup_field = 'slug'

    def get_queryset(self):
        return Produit.objects.visibles_publiquement().select_related(
            'boutique__proprietaire', 'categorie'
        ).prefetch_related(
            'images',
            Prefetch('categorie__sous_categories', queryset=Categorie.objects.actives()),
            Prefetch(
                'variantes',
                queryset=VarianteProduit.objects.filter(est_active=True).select_related('stock'),
                to_attr='variantes_actives',
            ),
        )


# =====================================================================
# VUES ESPACE VENDEUR (GESTION PRIVÉE DES PRODUITS)
# =====================================================================

def produits_du_vendeur(utilisateur):
    return Produit.objects.filter(boutique__proprietaire=utilisateur).select_related(
        'categorie', 'boutique__proprietaire'
    ).prefetch_related('images', 'variantes__stock')


class ProduitVendeurListCreateView(generics.ListCreateAPIView):
    """Liste et création des produits de la boutique du vendeur connecté."""
    permission_classes = PERMISSIONS_VENDEUR
    serializer_class = ProduitVendeurSerializer

    def get_queryset(self):
        return produits_du_vendeur(self.request.user)

    def perform_create(self, serializer):
        user = self.request.user
        if not hasattr(user, 'boutique'):
            raise ValidationError("Vous devez d'abord ouvrir une boutique avant d'ajouter des produits.")
        serializer.save(boutique=user.boutique)


class ProduitVendeurDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Consultation, mise à jour ou désactivation d'un produit par son propriétaire."""
    permission_classes = PERMISSIONS_VENDEUR + [EstProprietaireDuProduit]
    serializer_class = ProduitVendeurSerializer

    def get_queryset(self):
        return produits_du_vendeur(self.request.user)

    def perform_destroy(self, instance):
        """Désactivation, jamais de suppression : commandes, passeports,
        paniers et tickets restent liés au produit."""
        instance.desactiver(par=Produit.OrigineDesactivation.VENDEUR)


class VarianteVendeurListCreateView(generics.ListCreateAPIView):
    """Gestion des variantes d'un produit précis."""
    permission_classes = PERMISSIONS_VENDEUR
    serializer_class = VarianteVendeurCreateSerializer

    def _get_produit(self):
        return get_object_or_404(
            Produit,
            pk=self.kwargs['produit_pk'],
            boutique__proprietaire=self.request.user
        )

    def get_queryset(self):
        produit = self._get_produit()
        return VarianteProduit.objects.filter(produit=produit).select_related('stock')

    def perform_create(self, serializer):
        produit = self._get_produit()
        serializer.save(produit=produit)


class VarianteVendeurDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Mise à jour ou désactivation d'une variante par son propriétaire."""
    permission_classes = PERMISSIONS_VENDEUR + [EstProprietaireDeLaVariante]
    serializer_class = VarianteProduitSerializer

    def get_queryset(self):
        return VarianteProduit.objects.filter(
            produit__boutique__proprietaire=self.request.user
        ).select_related('stock', 'produit__boutique')

    def perform_destroy(self, instance):
        """Désactivation, jamais de suppression : une variante commandée,
        certifiée par un passeport ou dans un panier doit rester (le panier
        la marque alors indisponible)."""
        VarianteProduit.objects.filter(pk=instance.pk).update(est_active=False, date_mise_a_jour=timezone.now())


class StockUpdateView(generics.UpdateAPIView):
    """Mise à jour rapide du stock physique d'une variante (écriture partielle sous verrou)."""
    permission_classes = PERMISSIONS_VENDEUR + [EstProprietaireDeLaVariante]
    serializer_class = StockUpdateSerializer

    def get_object(self):
        variante = get_object_or_404(
            VarianteProduit.objects.select_related('produit__boutique'),
            pk=self.kwargs['pk'],
            produit__boutique__proprietaire=self.request.user
        )
        self.check_object_permissions(self.request, variante)
        return get_object_or_404(Stock, variante=variante)


class ImageProduitListCreateView(generics.ListCreateAPIView):
    """Ajout d'images à la galerie d'un produit (10 au maximum)."""
    permission_classes = PERMISSIONS_VENDEUR
    serializer_class = ImageProduitSerializer

    def _get_produit(self):
        return get_object_or_404(
            Produit,
            pk=self.kwargs['produit_pk'],
            boutique__proprietaire=self.request.user
        )

    def get_queryset(self):
        produit = self._get_produit()
        return ImageProduit.objects.filter(produit=produit)

    def perform_create(self, serializer):
        produit = self._get_produit()
        # Verrou sur le produit : deux envois simultanés ne peuvent pas
        # dépasser la limite ensemble.
        with transaction.atomic():
            Produit.objects.select_for_update().get(pk=produit.pk)
            if ImageProduit.objects.filter(produit=produit).count() >= MAX_IMAGES_PAR_PRODUIT:
                raise ValidationError({
                    'image': f"Un produit ne peut pas avoir plus de {MAX_IMAGES_PAR_PRODUIT} images. "
                             "Supprimez-en une avant d'en ajouter une autre."
                })
            serializer.save(produit=produit)


class ImageProduitDeleteView(generics.DestroyAPIView):
    """Suppression d'une image de la galerie d'un produit."""
    permission_classes = PERMISSIONS_VENDEUR + [EstProprietaireDeLImage]
    serializer_class = ImageProduitSerializer

    def get_queryset(self):
        return ImageProduit.objects.filter(
            produit__boutique__proprietaire=self.request.user
        ).select_related('produit__boutique')


# =====================================================================
# ADMINISTRATION (MODÉRATION)
# =====================================================================

class ProduitAdministrationDetailView(generics.RetrieveUpdateAPIView):
    """Modération d'un produit : désactivation / réactivation uniquement."""
    permission_classes = [IsAuthenticated, EstAdministrateur]
    serializer_class = ProduitAdministrationSerializer
    queryset = Produit.objects.select_related('boutique')

    def perform_update(self, serializer):
        etait_actif = serializer.instance.est_actif
        produit = serializer.save()
        if etait_actif != produit.est_actif:
            logger_securite.info(
                "Produit %s (%s) %s par admin_id=%s",
                produit.id, produit.nom, "réactivé" if produit.est_actif else "désactivé",
                self.request.user.id,
            )
