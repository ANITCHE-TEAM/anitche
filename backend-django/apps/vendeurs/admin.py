from django.contrib import admin, messages

from .models import Boutique, DemandeVendeur
from .services import (
    AutoApprobationInterdite,
    TransitionVendeurImpossible,
    refuser_demande_vendeur,
    valider_demande_vendeur,
)


@admin.register(Boutique)
class BoutiqueAdmin(admin.ModelAdmin):
    list_display = ('nom', 'proprietaire', 'ville', 'est_active', 'est_publiable', 'date_creation')
    list_filter = ('est_active', 'ville', 'proprietaire__statut_kyc')
    search_fields = ('nom', 'proprietaire__email', 'proprietaire__nom', 'ville')
    readonly_fields = ('slug', 'date_creation', 'date_mise_a_jour')
    autocomplete_fields = ('proprietaire',)

    @admin.display(boolean=True, description="Publiable")
    def est_publiable(self, obj):
        return obj.est_publiable


@admin.register(DemandeVendeur)
class DemandeVendeurAdmin(admin.ModelAdmin):
    """File de traitement des demandes vendeur (comptes en attente).

    Lecture seule : les décisions passent par les actions, qui appellent les
    mêmes services que l'API — pas d'édition manuelle du couple rôle/statut.
    """

    list_display = ('email', 'nom', 'prenom', 'telephone', 'statut_kyc', 'date_creation')
    search_fields = ('email', 'nom', 'prenom', 'telephone')
    ordering = ('date_creation',)
    actions = ('valider_les_demandes', 'refuser_les_demandes')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def _appliquer(self, request, queryset, service, libelle):
        traitees, echecs = 0, 0
        for demande in queryset:
            try:
                service(demande, decideur=request.user)
            except AutoApprobationInterdite as erreur:
                echecs += 1
                self.message_user(
                    request, f"{demande.email} : {erreur}", level=messages.WARNING
                )
            except TransitionVendeurImpossible as erreur:
                echecs += 1
                self.message_user(
                    request, f"{demande.email} : {erreur}", level=messages.WARNING
                )
            else:
                traitees += 1

        if traitees:
            self.message_user(request, f"{traitees} demande(s) {libelle}.", level=messages.SUCCESS)
        if not traitees and not echecs:
            self.message_user(request, "Aucune demande traitée.", level=messages.INFO)

    @admin.action(description="Valider les demandes sélectionnées")
    def valider_les_demandes(self, request, queryset):
        self._appliquer(request, queryset, valider_demande_vendeur, "validée(s)")

    @admin.action(description="Refuser les demandes sélectionnées")
    def refuser_les_demandes(self, request, queryset):
        self._appliquer(request, queryset, refuser_demande_vendeur, "refusée(s)")