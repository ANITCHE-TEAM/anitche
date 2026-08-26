from django.contrib import admin
from .models import Categorie, Produit, ImageProduit, VarianteProduit, Stock


class ImageProduitInline(admin.TabularInline):
    model = ImageProduit
    extra = 1


class VarianteProduitInline(admin.StackedInline):
    model = VarianteProduit
    extra = 1


@admin.register(Categorie)
class CategorieAdmin(admin.ModelAdmin):
    list_display = ('nom', 'slug', 'parent', 'est_active', 'ordre', 'date_creation')
    list_filter = ('est_active', 'parent')
    search_fields = ('nom', 'description')
    prepopulated_fields = {'slug': ('nom',)}


@admin.register(Produit)
class ProduitAdmin(admin.ModelAdmin):
    list_display = ('nom', 'boutique', 'categorie', 'prix_base', 'est_actif', 'date_creation')
    list_filter = ('est_actif', 'categorie', 'date_creation')
    search_fields = ('nom', 'description', 'boutique__nom')
    prepopulated_fields = {'slug': ('nom',)}
    inlines = [ImageProduitInline, VarianteProduitInline]


@admin.register(VarianteProduit)
class VarianteProduitAdmin(admin.ModelAdmin):
    list_display = ('nom', 'produit', 'sku', 'prix', 'prix_promo', 'est_active')
    list_filter = ('est_active',)
    search_fields = ('nom', 'sku', 'produit__nom')


@admin.register(Stock)
class StockAdmin(admin.ModelAdmin):
    list_display = ('variante', 'quantite_disponible', 'seuil_alerte', 'date_mise_a_jour')
    search_fields = ('variante__nom', 'variante__sku', 'variante__produit__nom')
