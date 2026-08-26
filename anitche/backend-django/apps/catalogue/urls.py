from django.urls import path
from .views import (
    CategorieListView,
    CategorieDetailView,
    ProduitPublicListView,
    ProduitPublicDetailView,
    ProduitVendeurListCreateView,
    ProduitVendeurDetailView,
    VarianteVendeurListCreateView,
    VarianteVendeurDetailView,
    StockUpdateView,
    ImageProduitListCreateView,
    ImageProduitDeleteView,
)

app_name = 'catalogue'

urlpatterns = [
    # --- Endpoints Publics ---
    path('categories/', CategorieListView.as_view(), name='categories-liste'),
    path('categories/<slug:slug>/', CategorieDetailView.as_view(), name='categorie-detail'),
    path('produits/', ProduitPublicListView.as_view(), name='produits-liste'),
    path('produits/<slug:slug>/', ProduitPublicDetailView.as_view(), name='produit-detail'),

    # --- Endpoints Espace Vendeur ---
    path('vendeur/produits/', ProduitVendeurListCreateView.as_view(), name='vendeur-produits-liste'),
    path('vendeur/produits/<int:pk>/', ProduitVendeurDetailView.as_view(), name='vendeur-produit-detail'),
    path('vendeur/produits/<int:produit_pk>/variantes/', VarianteVendeurListCreateView.as_view(), name='vendeur-variantes-liste'),
    path('vendeur/variantes/<int:pk>/', VarianteVendeurDetailView.as_view(), name='vendeur-variante-detail'),
    path('vendeur/variantes/<int:pk>/stock/', StockUpdateView.as_view(), name='vendeur-stock-update'),
    path('vendeur/produits/<int:produit_pk>/images/', ImageProduitListCreateView.as_view(), name='vendeur-images-liste'),
    path('vendeur/images/<int:pk>/', ImageProduitDeleteView.as_view(), name='vendeur-image-detail'),
]
