from django.urls import path
from .views import (
    ValiderPanierView,
    GroupeCommandeListView,
    CommandeListView,
    CommandeDetailView,
    CommandeItemListView,
)

app_name = "commandes"

urlpatterns = [
    path("valider-panier/", ValiderPanierView.as_view(), name="valider-panier"),
    path("groupes/", GroupeCommandeListView.as_view(), name="groupe-list"),
    path("", CommandeListView.as_view(), name="commande-list"),
    path("<uuid:pk>/", CommandeDetailView.as_view(), name="commande-detail"),
    path("<uuid:commande_id>/items/", CommandeItemListView.as_view(), name="commande-items"),
]