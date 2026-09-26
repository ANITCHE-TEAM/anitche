from django.urls import path
from .views import (
    ValiderPanierView,
    GroupeCommandeListView,
    CommandeListView,
    CommandeDetailView,
    CommandeItemListView,
    AnnulerCommandeView,
    CommandeVendeurListView,
    CommandeVendeurDetailView,
    PasserEnPreparationView,
    AnnulerCommandeAdministrationView,
)

app_name = "commandes"

urlpatterns = [
    path("valider-panier/", ValiderPanierView.as_view(), name="valider-panier"),
    path("groupes/", GroupeCommandeListView.as_view(), name="groupe-list"),

    # --- Espace vendeur ---
    path("vendeur/", CommandeVendeurListView.as_view(), name="vendeur-commande-list"),
    path("vendeur/<uuid:pk>/", CommandeVendeurDetailView.as_view(), name="vendeur-commande-detail"),
    path("vendeur/<uuid:pk>/preparation/", PasserEnPreparationView.as_view(), name="vendeur-commande-preparation"),

    # --- Administration ---
    path("administration/<uuid:pk>/annuler/", AnnulerCommandeAdministrationView.as_view(), name="administration-commande-annuler"),

    # --- Client ---
    path("", CommandeListView.as_view(), name="commande-list"),
    path("<uuid:pk>/", CommandeDetailView.as_view(), name="commande-detail"),
    path("<uuid:pk>/annuler/", AnnulerCommandeView.as_view(), name="commande-annuler"),
    path("<uuid:commande_id>/items/", CommandeItemListView.as_view(), name="commande-items"),
]
