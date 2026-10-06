from django.contrib import admin

from .models import CompteFidelite, CouponReduction, GainFidelite, TransactionFidelite, UtilisationCoupon


class LectureSeuleAdmin(admin.ModelAdmin):
    """Journal d'audit : consultable, jamais créé, modifié ni supprimé à la
    main (un mouvement de points se fait par apps.fidelite.services, qui
    écrit la transaction correspondante)."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CompteFidelite)
class CompteFideliteAdmin(admin.ModelAdmin):
    list_display = ("utilisateur", "solde_points", "points_cumules_total", "palier", "date_creation")
    list_filter = ("palier", "date_creation")
    search_fields = ("utilisateur__email",)
    # Solde et palier en lecture seule : modifier le solde ici fabriquerait
    # des points sans transaction d'audit.
    readonly_fields = ("id", "utilisateur", "solde_points", "points_cumules_total", "palier",
                       "date_creation", "date_mise_a_jour")

    def has_add_permission(self, request):
        return False


@admin.register(TransactionFidelite)
class TransactionFideliteAdmin(LectureSeuleAdmin):
    list_display = ("compte", "type_transaction", "points", "solde_apres", "reference_externe", "date_creation")
    list_filter = ("type_transaction", "date_creation")
    search_fields = ("compte__utilisateur__email", "description", "reference_externe")


@admin.register(GainFidelite)
class GainFideliteAdmin(LectureSeuleAdmin):
    list_display = ("compte", "commande", "points", "statut", "date_disponibilite", "date_traitement")
    list_filter = ("statut",)
    search_fields = ("compte__utilisateur__email", "commande__numero_commande")


@admin.register(UtilisationCoupon)
class UtilisationCouponAdmin(LectureSeuleAdmin):
    list_display = ("coupon", "client", "groupe", "date_creation")
    search_fields = ("coupon__code", "client__email")


@admin.register(CouponReduction)
class CouponReductionAdmin(admin.ModelAdmin):
    """Création de codes promo par l'administration. `utilisations_max` vide =
    illimité (chaque client une fois) ; 1 par défaut."""

    list_display = ("code", "client", "type_reduction", "valeur", "montant_minimum_commande", "est_actif",
                    "nombre_utilisations", "utilisations_max", "est_utilise", "date_expiration")
    list_filter = ("type_reduction", "est_actif", "est_utilise", "date_expiration")
    search_fields = ("code", "client__email")
    # Compteurs tenus par le checkout (apps.fidelite.services.consommer_coupon).
    readonly_fields = ("id", "date_creation", "nombre_utilisations", "est_utilise", "points_requis")
