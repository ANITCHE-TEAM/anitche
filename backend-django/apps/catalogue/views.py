from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated

from apps.vendeurs.permissions import EstVendeurValide
from .models import Categorie, Produit, VarianteProduit, ImageProduit, Stock
from .permissions import (
    EstProprietaireDuProduit,
    EstProprietaireDeLaVariante,
    EstProprietaireDeLImage,
)
from .serializers import (
    CategorieSerializer,
    ProduitPublicListSerializer,
    ProduitPublicDetailSerializer,
    ProduitVendeurSerializer,
    VarianteProduitSerializer,
    VarianteVendeurCreateSerializer,
    ImageProduitSerializer,
    StockUpdateSerializer,
)


# =====================================================================
# VUES PUBLIQUES (CLIENTS / VISITEURS)
# =====================================================================

class CategorieListView(generics.ListAPIView):
    """Liste de toutes les catégories principales et leurs sous-catégories actives."""
    permission_classes = [AllowAny]
    serializer_class = CategorieSerializer
    pagination_class = None

    def get_queryset(self):
        return Categorie.objects.actives().racines().prefetch_related('sous_categories')


class CategorieDetailView(generics.RetrieveAPIView):
    """Détail d'une catégorie identifiée par son slug."""
    permission_classes = [AllowAny]
    serializer_class = CategorieSerializer
    lookup_field = 'slug'

    def get_queryset(self):
        return Categorie.objects.actives().prefetch_related('sous_categories')


class ProduitPublicListView(generics.ListAPIView):
    """Recherche et filtrage des produits publiables sur la marketplace."""
    permission_classes = [AllowAny]
    serializer_class = ProduitPublicListSerializer

    def get_queryset(self):
        queryset = Produit.objects.publies().select_related(
            'boutique', 'categorie'
        ).prefetch_related(
            'images', 'variantes__stock'
        )

        params = self.request.query_params

        # Filtrage par recherche texte
        recherche = params.get('recherche')
        if recherche:
            queryset = queryset.filter(
                Q(nom__icontains=recherche) |
                Q(description__icontains=recherche) |
                Q(boutique__nom__icontains=recherche)
            )

        # Filtrage par catégorie (slug ou id)
        categorie = params.get('categorie')
        if categorie:
            if categorie.isdigit():
                queryset = queryset.filter(
                    Q(categorie_id=categorie) | Q(categorie__parent_id=categorie)
                )
            else:
                queryset = queryset.filter(
                    Q(categorie__slug=categorie) | Q(categorie__parent__slug=categorie)
                )

        # Filtrage par boutique (slug ou id)
        boutique = params.get('boutique')
        if boutique:
            if boutique.isdigit():
                queryset = queryset.filter(boutique_id=boutique)
            else:
                queryset = queryset.filter(boutique__slug=boutique)

        # Filtrage par fourchette de prix (sur prix_base ou variantes)
        prix_min = params.get('prix_min')
        if prix_min and prix_min.isdigit():
            queryset = queryset.filter(
                Q(prix_base__gte=prix_min) | Q(variantes__prix__gte=prix_min)
            ).distinct()

        prix_max = params.get('prix_max')
        if prix_max and prix_max.isdigit():
            queryset = queryset.filter(
                Q(prix_base__lte=prix_max) | Q(variantes__prix__lte=prix_max)
            ).distinct()

        # Tri
        tri = params.get('tri')
        if tri == 'prix_asc':
            queryset = queryset.order_by('prix_base')
        elif tri == 'prix_desc':
            queryset = queryset.order_by('-prix_base')
        elif tri == 'date_asc':
            queryset = queryset.order_by('date_creation')
        else:
            queryset = queryset.order_by('-date_creation')

        return queryset


class ProduitPublicDetailView(generics.RetrieveAPIView):
    """Fiche produit détaillée (variantes, stocks, images, boutique)."""
    permission_classes = [AllowAny]
    serializer_class = ProduitPublicDetailSerializer
    lookup_field = 'slug'

    def get_queryset(self):
        return Produit.objects.publies().select_related(
            'boutique', 'categorie'
        ).prefetch_related(
            'images', 'variantes__stock'
        )


# =====================================================================
# VUES ESPACE VENDEUR (GESTION PRIVÉE DES PRODUITS)
# =====================================================================

class ProduitVendeurListCreateView(generics.ListCreateAPIView):
    """Liste et création des produits de la boutique du vendeur connecté."""
    permission_classes = [IsAuthenticated, EstVendeurValide]
    serializer_class = ProduitVendeurSerializer

    def get_queryset(self):
        return Produit.objects.filter(
            boutique__proprietaire=self.request.user
        ).select_related('categorie').prefetch_related('images', 'variantes__stock')

    def perform_create(self, serializer):
        user = self.request.user
        if not hasattr(user, 'boutique'):
            raise ValidationError("Vous devez d'abord ouvrir une boutique avant d'ajouter des produits.")
        serializer.save(boutique=user.boutique)


class ProduitVendeurDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Consultation, mise à jour ou suppression d'un produit par son propriétaire."""
    permission_classes = [IsAuthenticated, EstVendeurValide, EstProprietaireDuProduit]
    serializer_class = ProduitVendeurSerializer

    def get_queryset(self):
        return Produit.objects.filter(boutique__proprietaire=self.request.user)


class VarianteVendeurListCreateView(generics.ListCreateAPIView):
    """Gestion des variantes d'un produit précis."""
    permission_classes = [IsAuthenticated, EstVendeurValide]
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
    """Mise à jour ou suppression d'une variante par son propriétaire."""
    permission_classes = [IsAuthenticated, EstVendeurValide, EstProprietaireDeLaVariante]
    serializer_class = VarianteProduitSerializer

    def get_queryset(self):
        return VarianteProduit.objects.filter(
            produit__boutique__proprietaire=self.request.user
        ).select_related('stock')


class StockUpdateView(generics.UpdateAPIView):
    """Mise à jour rapide du stock physique d'une variante."""
    permission_classes = [IsAuthenticated, EstVendeurValide, EstProprietaireDeLaVariante]
    serializer_class = StockUpdateSerializer

    def get_object(self):
        variante = get_object_or_404(
            VarianteProduit,
            pk=self.kwargs['pk'],
            produit__boutique__proprietaire=self.request.user
        )
        return get_object_or_404(Stock, variante=variante)


class ImageProduitListCreateView(generics.ListCreateAPIView):
    """Ajout d'images à la galerie d'un produit."""
    permission_classes = [IsAuthenticated, EstVendeurValide]
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
        serializer.save(produit=produit)


class ImageProduitDeleteView(generics.DestroyAPIView):
    """Suppression d'une image de la galerie d'un produit."""
    permission_classes = [IsAuthenticated, EstVendeurValide, EstProprietaireDeLImage]
    serializer_class = ImageProduitSerializer

    def get_queryset(self):
        return ImageProduit.objects.filter(
            produit__boutique__proprietaire=self.request.user
        )
