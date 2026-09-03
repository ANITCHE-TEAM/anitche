from django.contrib import admin
from .models import DemandeRetour, RetourItem, PhotoRetour


class RetourItemInline(admin.TabularInline):
    model = RetourItem
    extra = 0
    readonly_fields = ("commande_item", "quantite")


class PhotoRetourInline(admin.TabularInline):
    model = PhotoRetour
    extra = 0
    readonly_fields = ("image", "date_ajout")


@admin.register(DemandeRetour)
class DemandeRetourAdmin(admin.ModelAdmin):
    list_display = (
        "numero_retour",
        "commande",
        "client",
        "boutique",
        "motif",
        "type_resolution",
        "statut",
        "montant_remboursement",
        "date_creation",
    )
    list_filter = ("statut", "motif", "type_resolution", "date_creation")
    search_fields = ("numero_retour", "commande__numero_commande", "client__email", "boutique__nom")
    readonly_fields = ("id", "numero_retour", "date_creation", "date_traitement", "date_cloture", "date_mise_a_jour")
    inlines = [RetourItemInline, PhotoRetourInline]
    ordering = ("-date_creation",)


@admin.register(RetourItem)
class RetourItemAdmin(admin.ModelAdmin):
    list_display = ("demande_retour", "commande_item", "quantite")
    search_fields = ("demande_retour__numero_retour", "commande_item__nom_produit")


@admin.register(PhotoRetour)
class PhotoRetourAdmin(admin.ModelAdmin):
    list_display = ("demande_retour", "image", "date_ajout")
