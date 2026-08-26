from django.contrib import admin
from .models import Commande, GroupeCommande, CommandeItem


class CommandeItemInline(admin.TabularInline):
    model = CommandeItem
    extra = 0
    readonly_fields = ("variante", "nom_produit", "prix_unitaire", "quantite")
    can_delete = False


@admin.register(Commande)
class CommandeAdmin(admin.ModelAdmin):
    list_display = ("numero_commande", "client", "boutique", "status", "montant_total", "created_at")
    list_filter = ("status", "boutique")
    search_fields = ("numero_commande", "client__email", "boutique__nom")
    readonly_fields = ("id", "numero_commande", "created_at", "update_at")
    inlines = [CommandeItemInline]


@admin.register(GroupeCommande)
class GroupeCommandeAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "created_at")
    search_fields = ("client__email",)
    readonly_fields = ("id", "created_at")


@admin.register(CommandeItem)
class CommandeItemAdmin(admin.ModelAdmin):
    list_display = ("commande", "nom_produit", "variante", "prix_unitaire", "quantite")
    search_fields = ("nom_produit", "commande__numero_commande")
    readonly_fields = ("id",)