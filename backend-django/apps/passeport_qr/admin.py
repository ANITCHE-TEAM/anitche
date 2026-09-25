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
        "desactive_par",
    )
    list_filter = ("statut_certification", "est_actif", "desactive_par", "date_creation")
    search_fields = ("code_passeport", "produit__nom", "boutique__nom", "numero_lot", "artisan_createur")
    # url_verification_publique est une propriété calculée (affichage seul).
    readonly_fields = ("id", "code_passeport", "nb_scans", "dernier_scan", "desactive_par", "url_verification_publique", "date_creation", "date_mise_a_jour")
    inlines = [HistoriqueScanInline]
    ordering = ("-date_creation",)

    def save_model(self, request, obj, form, change):
        """Une (dés)activation depuis le Django admin est une décision de
        l'administration : même règle que l'API (desactive_par cohérent)."""
        if "est_actif" in form.changed_data:
            obj.desactive_par = "" if obj.est_actif else PasseportProduit.OrigineDesactivation.ADMINISTRATION
        super().save_model(request, obj, form, change)


@admin.register(HistoriqueScanPasseport)
class HistoriqueScanPasseportAdmin(admin.ModelAdmin):
    list_display = ("passeport", "adresse_ip", "date_scan")
    list_filter = ("date_scan",)
    search_fields = ("passeport__code_passeport", "adresse_ip")
    readonly_fields = ("id", "passeport", "adresse_ip", "user_agent", "date_scan")
