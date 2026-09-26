from django.contrib import admin
from .models import Paiement, JournalWebhook


@admin.register(Paiement)
class PaiementAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "client",
        "methode",
        "montant",
        "devise",
        "statut",
        "transaction_id_externe",
        "date_creation",
        "date_validation",
    )
    list_filter = ("statut", "methode", "devise", "date_creation")
    search_fields = ("reference", "client__email", "transaction_id_externe", "commande__numero_commande")
    readonly_fields = (
        "id", "reference", "statut",
        "date_creation", "date_validation", "date_mise_a_jour",
    )
    # 'statut' en lecture seule (F-14) : toute validation doit passer par
    # ServicePaiement.traiter_webhook (HMAC, vérification du montant reçu,
    # idempotence) — une édition libre ici permettrait à un compte staff
    # de marquer un paiement VALIDE sans qu'aucun argent n'ait transité.
    ordering = ("-date_creation",)


@admin.register(JournalWebhook)
class JournalWebhookAdmin(admin.ModelAdmin):
    list_display = ("fournisseur", "evenement_id", "statut_traitement", "date_reception")
    list_filter = ("fournisseur", "statut_traitement", "date_reception")
    search_fields = ("evenement_id", "fournisseur")
    readonly_fields = ("id", "date_reception")
    ordering = ("-date_reception",)