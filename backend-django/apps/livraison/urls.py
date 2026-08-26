from django.urls import path
from .views import (
    LivraisonListView,
    LivraisonDetailView,
    LivraisonChangerStatusView,
    LivraisonHistoriqueListView,
)

app_name = "livraison"

urlpatterns = [
    path("", LivraisonListView.as_view(), name="livraison-list"),
    path("<uuid:pk>/", LivraisonDetailView.as_view(), name="livraison-detail"),
    path("<uuid:pk>/statut/", LivraisonChangerStatusView.as_view(), name="livraison-changer-status"),
    path("<uuid:livraison_id>/historique/", LivraisonHistoriqueListView.as_view(), name="livraison-historique"),
]