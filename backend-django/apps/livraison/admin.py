from django import forms
from django.contrib import admin

from apps.utilisateurs.models import Role, Utilisateur
from apps.vendeurs.permissions import ROLES_ADMINISTRATION

from . import services
from .models import ContestationLivraison, Livraison, LivraisonHistorique


class LivraisonHistoriqueInline(admin.TabularInline):
    model = LivraisonHistorique
    extra = 0
    readonly_fields = ["ancien_status", "nouveau_status", "effectue_par", "role_acteur", "commentaire", "created_at"]
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class LivraisonAdminForm(forms.ModelForm):
    """Seuls les livreurs actifs sont proposés ; même règle que l'API
    (jamais le propriétaire de la boutique de la commande)."""

    class Meta:
        model = Livraison
        fields = ["livreur", "date_livraison_estimee"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["livreur"].queryset = Utilisateur.objects.filter(role=Role.LIVREUR, is_active=True)

    def clean(self):
        donnees = super().clean()
        livreur = donnees.get("livreur")
        if self.instance.pk and "livreur" in self.changed_data:
            if livreur is None:
                raise forms.ValidationError("Une livraison assignée ne peut pas être désassignée : réassignez-la.")
            if self.instance.status in services.STATUTS_TERMINES:
                raise forms.ValidationError("Livraison terminée : elle ne peut plus être assignée.")
            motif = services.livreur_eligible(livreur, self.instance.commande)
            if motif:
                raise forms.ValidationError(motif)
        return donnees


@admin.register(Livraison)
class LivraisonAdmin(admin.ModelAdmin):
    form = LivraisonAdminForm
    list_display = ["id", "commande", "livreur", "status", "date_livraison_estimee", "date_livraison", "created_at"]
    list_filter = ["status"]
    search_fields = ["commande__numero_commande", "adresse_livraison"]
    # Le statut ne change que par apps.livraison.services (API) : seuls le
    # livreur et la date estimée s'éditent ici.
    readonly_fields = [
        "id", "commande", "status", "adresse_livraison", "date_expedition", "date_livraison",
        "tentatives", "code_essais", "created_at", "updated_at",
    ]
    fields = ["livreur", "date_livraison_estimee", *readonly_fields]
    inlines = [LivraisonHistoriqueInline]

    def has_add_permission(self, request):
        # Créée à la confirmation de la commande (paiement validé).
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        # is_staff seul ne donne aucun pouvoir métier.
        return super().has_change_permission(request, obj) and request.user.role in ROLES_ADMINISTRATION

    def save_model(self, request, obj, form, change):
        if "livreur" in form.changed_data:
            services.assigner_livreur(obj, form.cleaned_data["livreur"], request.user,
                                      date_livraison_estimee=form.cleaned_data.get("date_livraison_estimee"))
        elif "date_livraison_estimee" in form.changed_data:
            obj.save(update_fields=["date_livraison_estimee", "updated_at"])


@admin.register(LivraisonHistorique)
class LivraisonHistoriqueAdmin(admin.ModelAdmin):
    list_display = ["livraison", "ancien_status", "nouveau_status", "role_acteur", "effectue_par", "created_at"]
    list_filter = ["nouveau_status"]
    readonly_fields = [
        "id", "livraison", "ancien_status", "nouveau_status", "effectue_par", "role_acteur", "commentaire", "created_at",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ContestationLivraison)
class ContestationLivraisonAdmin(admin.ModelAdmin):
    """Consultation ; la décision passe par l'API (reversement, remboursement)."""
    list_display = ["livraison", "statut", "date_creation", "date_resolution"]
    list_filter = ["statut"]
    readonly_fields = [
        "id", "livraison", "motif", "statut", "commentaire_resolution", "resolue_par", "date_creation", "date_resolution",
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
