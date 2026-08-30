from django.contrib import admin
from .models import PasseportProduit, HistoriqueScanPasseport


class HistoriqueScanInline(admin.TabularInline):
    model = HistoriqueScanPasseport
    extra = 0
    readonly_fields = ("adresse_ip", "user_agent", "date_scan")


@admin.register(PasseportProduit)
class PasseportProduitAdmin(admin.ModelAdmin):
    list_display = (
        "code_passeport",
        "produit",
        "boutique",
        "numero_lot",
        "statut_certification",
        "nb_scans",
        "dernier_scan",
        "est_actif",
    )
    list_filter = ("statut_certification", "est_actif", "date_creation")
    search_fields = ("code_passeport", "produit__nom", "boutique__nom", "numero_lot", "artisan_createur")
    readonly_fields = ("id", "code_passeport", "nb_scans", "dernier_scan", "url_verification_publique", "date_creation", "date_mise_a_jour")
    inlines = [HistoriqueScanInline]
    ordering = ("-date_creation",)


@admin.register(HistoriqueScanPasseport)
class HistoriqueScanPasseportAdmin(admin.ModelAdmin):
    list_display = ("passeport", "adresse_ip", "date_scan")
    list_filter = ("date_scan",)
    search_fields = ("passeport__code_passeport", "adresse_ip")
    readonly_fields = ("id", "passeport", "adresse_ip", "user_agent", "date_scan")
