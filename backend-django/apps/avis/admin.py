from django.contrib import admin

from .models import Avis


@admin.register(Avis)
class AvisAdmin(admin.ModelAdmin):
    """Lecture seule : le statut et les agrégats ne changent que par
    apps.avis.services (un changement ici laisserait la note du produit
    fausse)."""

    list_display = ("produit", "client", "note", "statut", "est_modifie", "date_creation")
    list_filter = ("statut", "note")
    search_fields = ("produit__nom", "client__email", "boutique__nom")
    readonly_fields = [champ.name for champ in Avis._meta.fields]
    ordering = ("-date_creation",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
