from django.contrib import admin

from .models import Avis, DecisionModeration, ReponseVendeur, SignalementAvis


class LectureSeuleAdmin(admin.ModelAdmin):
    """Lecture seule : statuts, réponses, signalements et décisions ne changent
    que par apps.avis.services (un changement ici laisserait la note du produit
    ou la trace de modération fausses)."""

    def get_readonly_fields(self, request, obj=None):
        return [champ.name for champ in self.model._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Avis)
class AvisAdmin(LectureSeuleAdmin):
    list_display = ("produit", "client", "note", "statut", "est_modifie", "date_creation")
    list_filter = ("statut", "note")
    search_fields = ("produit__nom", "client__email", "boutique__nom")
    ordering = ("-date_creation",)


@admin.register(ReponseVendeur)
class ReponseVendeurAdmin(LectureSeuleAdmin):
    list_display = ("avis", "auteur", "date_creation", "date_modification")
    search_fields = ("auteur__email",)


@admin.register(SignalementAvis)
class SignalementAvisAdmin(LectureSeuleAdmin):
    list_display = ("avis", "auteur", "motif", "statut", "date_creation")
    list_filter = ("statut", "motif")
    search_fields = ("auteur__email",)


@admin.register(DecisionModeration)
class DecisionModerationAdmin(LectureSeuleAdmin):
    list_display = ("avis", "type_decision", "moderateur", "date_creation")
    list_filter = ("type_decision",)
    search_fields = ("moderateur__email",)
