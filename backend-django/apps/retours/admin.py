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
    # 'statut' et 'montant_remboursement' en lecture seule (F-06) : toute
    # transition doit passer par TraiterDemandeRetourView, sinon on
    # contourne sa machine à états (ex. passer directement à "rembourse"
    # sans déclencher le remboursement réel).
    readonly_fields = (
        "id", "numero_retour", "date_creation", "date_traitement", "date_cloture", "date_mise_a_jour",
        "statut", "montant_remboursement",
    )
    inlines = [RetourItemInline, PhotoRetourInline]
    ordering = ("-date_creation",)


@admin.register(RetourItem)
class RetourItemAdmin(admin.ModelAdmin):
    list_display = ("demande_retour", "commande_item", "quantite")
    search_fields = ("demande_retour__numero_retour", "commande_item__nom_produit")
    # 'quantite' verrouillée : montant_remboursement est calculé UNE SEULE
    # FOIS à la création de la demande à partir de cette valeur (voir
    # CreerDemandeRetourSerializer.validate) — la modifier après coup
    # désynchronise le montant remboursé de ce qui a réellement été
    # déclaré retourné. Pire : DemandeRetour.receptionner() réutilise
    # cette même quantite pour réintégrer le stock — une valeur trafiquée
    # fausserait aussi le stock réel (même principe que le verrouillage de
    # CommandeItem dans apps.commandes.admin).
    readonly_fields = ("id", "demande_retour", "commande_item", "quantite")


@admin.register(PhotoRetour)
class PhotoRetourAdmin(admin.ModelAdmin):
    list_display = ("demande_retour", "image", "date_ajout")
    # 'image' verrouillée : c'est une preuve justificative dans un litige
    # (produit défectueux, etc.) — permettre son remplacement après coup
    # ouvrirait une possibilité de falsification de preuve par un compte
    # staff compromis ou malveillant.
    readonly_fields = ("id", "demande_retour", "image", "date_ajout")