"""Django admin en lecture seule.

L'argent ne bouge que par apps.paiements.services / reversements (verrous,
contrôles, journal de sécurité) et leurs endpoints réservés au rôle
administrateur. `is_staff` ne donne accès qu'à la consultation (F-14) :
un compte staff « technique » ne peut ni valider un paiement, ni marquer
un remboursement effectué, ni changer un taux de commission.
"""

from django.contrib import admin

from .models import AjustementVendeur, BaremeFrais, JournalWebhook, Paiement, Remboursement, Reversement


class LectureSeuleAdmin(admin.ModelAdmin):
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Paiement)
class PaiementAdmin(LectureSeuleAdmin):
    list_display = ("reference", "client", "methode", "fournisseur", "montant", "statut", "date_creation",
                    "date_validation")
    list_filter = ("statut", "methode", "fournisseur", "date_creation")
    search_fields = ("reference", "client__email", "transaction_id_externe")
    exclude = ("hash_jeton_notification",)
    ordering = ("-date_creation",)


@admin.register(JournalWebhook)
class JournalWebhookAdmin(LectureSeuleAdmin):
    list_display = ("fournisseur", "evenement_id", "statut_traitement", "date_reception")
    list_filter = ("fournisseur", "statut_traitement", "date_reception")
    search_fields = ("evenement_id", "fournisseur")
    ordering = ("-date_reception",)


@admin.register(Remboursement)
class RemboursementAdmin(LectureSeuleAdmin):
    list_display = ("reference", "commande", "montant", "motif", "statut", "date_creation", "date_traitement")
    list_filter = ("statut", "motif")
    search_fields = ("reference", "commande__numero_commande", "reference_externe")


@admin.register(Reversement)
class ReversementAdmin(LectureSeuleAdmin):
    list_display = ("reference", "boutique", "commande", "montant_net", "statut", "date_disponibilite",
                    "date_versement")
    list_filter = ("statut",)
    search_fields = ("reference", "commande__numero_commande", "boutique__nom")
    exclude = ("hash_jeton_notification", "numero_destinataire")


@admin.register(AjustementVendeur)
class AjustementVendeurAdmin(LectureSeuleAdmin):
    list_display = ("boutique", "nature", "montant", "motif", "reversement_impute", "date_creation")
    list_filter = ("nature",)


@admin.register(BaremeFrais)
class BaremeFraisAdmin(LectureSeuleAdmin):
    list_display = ("__str__", "boutique", "date_debut", "date_fin")
    list_filter = ("boutique",)
