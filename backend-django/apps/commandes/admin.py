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
    # 'status' et 'montant_total' en lecture seule : le statut d'une
    # commande ne doit changer que via le flux réel (paiement validé →
    # signal paiement_valide → confirmation, ou transitions de livraison)
    # — jamais par une édition libre dans l'admin, qui contournerait tout
    # contrôle de paiement (même principe que Paiement.statut et
    # Livraison.status, non modifiables dans leurs admins respectifs).
    # Frais de livraison figés au checkout : inclus dans le montant payé et
    # dans le reversement (livraison offerte), jamais modifiables après coup.
    readonly_fields = (
        "id", "numero_commande", "status", "montant_total", "frais_livraison", "livraison_offerte",
        "frais_livraison_vendeur", "created_at", "update_at",
    )
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
    # CommandeItem est un snapshot de ce qui a été réellement commandé et
    # facturé à l'instant T : l'éditer après coup romprait la cohérence
    # avec Commande.montant_total (calculé une seule fois à la création à
    # partir de ces mêmes lignes, lui aussi en lecture seule) et avec
    # Paiement.montant. Une correction légitime passe par un
    # remboursement/avoir, jamais par une réécriture silencieuse de
    # l'historique.
    readonly_fields = ("id", "commande", "variante", "nom_produit", "prix_unitaire", "quantite")