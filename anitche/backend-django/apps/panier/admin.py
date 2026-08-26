from django.contrib import admin
from .models import Panier, PanierItem


class PanierItemInline(admin.TabularInline):
    model = PanierItem
    extra = 0
    readonly_fields = ("added_at",)


@admin.register(Panier)
class PanierAdmin(admin.ModelAdmin):
    list_display = ("id", "utilisateur", "session_key", "nombre_articles", "total", "updated_at")
    list_filter = ("created_at",)
    search_fields = ("utilisateur__email", "session_key")
    readonly_fields = ("id", "created_at", "updated_at")
    inlines = [PanierItemInline]


@admin.register(PanierItem)
class PanierItemAdmin(admin.ModelAdmin):
    list_display = ("panier", "variante", "quantite", "sous_total", "added_at")
    search_fields = ("variante__nom", "variante__sku", "panier__utilisateur__email")
    readonly_fields = ("id", "added_at")