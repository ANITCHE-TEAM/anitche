from django.contrib import admin
from .models import Livraison, LivraisonHistorique


class LivraisonHistoriqueInline(admin.TabularInline):
    model = LivraisonHistorique
    extra = 0
    readonly_fields = ["ancien_status", "nouveau_status", "effectue_par", "commentaire", "created_at"]
    can_delete = False


@admin.register(Livraison)
class LivraisonAdmin(admin.ModelAdmin):
    list_display = ["id", "commande", "livreur", "status", "date_expedition", "date_livraison", "created_at"]
    list_filter = ["status"]
    search_fields = ["commande__numero_commande", "adresse_livraison"]
    readonly_fields = ["id", "created_at", "updated_at"]
    inlines = [LivraisonHistoriqueInline]


@admin.register(LivraisonHistorique)
class LivraisonHistoriqueAdmin(admin.ModelAdmin):
    list_display = ["livraison", "ancien_status", "nouveau_status", "effectue_par", "created_at"]
    list_filter = ["nouveau_status"]
    readonly_fields = ["id", "livraison", "ancien_status", "nouveau_status", "effectue_par", "commentaire", "created_at"]