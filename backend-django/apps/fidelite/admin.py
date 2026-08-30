from django.contrib import admin
from .models import CompteFidelite, TransactionFidelite, CouponReduction


@admin.register(CompteFidelite)
class CompteFideliteAdmin(admin.ModelAdmin):
    list_display = ("utilisateur", "solde_points", "points_cumules_total", "palier", "date_creation")
    list_filter = ("palier", "date_creation")
    search_fields = ("utilisateur__email",)
    readonly_fields = ("id", "points_cumules_total", "date_creation", "date_mise_a_jour")


@admin.register(TransactionFidelite)
class TransactionFideliteAdmin(admin.ModelAdmin):
    list_display = ("compte", "type_transaction", "points", "solde_apres", "reference_externe", "date_creation")
    list_filter = ("type_transaction", "date_creation")
    search_fields = ("compte__utilisateur__email", "description", "reference_externe")
    readonly_fields = ("id", "compte", "type_transaction", "points", "solde_apres", "date_creation")


@admin.register(CouponReduction)
class CouponReductionAdmin(admin.ModelAdmin):
    list_display = ("code", "client", "type_reduction", "valeur", "montant_minimum_commande", "est_actif", "est_utilise", "date_expiration")
    list_filter = ("type_reduction", "est_actif", "est_utilise", "date_expiration")
    search_fields = ("code", "client__email")
    readonly_fields = ("id", "date_creation")
