from django.urls import path
from .views import (
    AbandonnerLivraisonView,
    AssignerLivreurView,
    ContesterLivraisonView,
    LivraisonChangerStatusView,
    LivraisonDetailView,
    LivraisonHistoriqueListView,
    LivraisonListView,
    LivraisonVendeurDetailView,
    LivraisonVendeurListView,
    LivreurListView,
    NommerLivreurView,
    ResoudreContestationView,
    RetirerLivreurView,
    TarifLivraisonDetailView,
    TarifLivraisonListCreateView,
    TarifLivraisonPublicListView,
)

app_name = "livraison"

urlpatterns = [
    path("", LivraisonListView.as_view(), name="livraison-list"),

    # --- Espace vendeur (lecture seule) ---
    path("vendeur/", LivraisonVendeurListView.as_view(), name="vendeur-livraison-list"),
    path("vendeur/<uuid:pk>/", LivraisonVendeurDetailView.as_view(), name="vendeur-livraison-detail"),

    # --- Frais de livraison ---
    path("tarifs/", TarifLivraisonPublicListView.as_view(), name="tarif-list"),
    path("admin/tarifs/", TarifLivraisonListCreateView.as_view(), name="admin-tarif-list"),
    path("admin/tarifs/<uuid:pk>/", TarifLivraisonDetailView.as_view(), name="admin-tarif-detail"),

    # --- Administration : livreurs ---
    path("livreurs/", LivreurListView.as_view(), name="livreur-list"),
    path("livreurs/nommer/", NommerLivreurView.as_view(), name="livreur-nommer"),
    path("livreurs/<int:pk>/retirer/", RetirerLivreurView.as_view(), name="livreur-retirer"),

    # --- Livraison ---
    path("<uuid:pk>/", LivraisonDetailView.as_view(), name="livraison-detail"),
    path("<uuid:pk>/statut/", LivraisonChangerStatusView.as_view(), name="livraison-changer-status"),
    path("<uuid:livraison_id>/historique/", LivraisonHistoriqueListView.as_view(), name="livraison-historique"),
    path("<uuid:pk>/contester/", ContesterLivraisonView.as_view(), name="livraison-contester"),
    path("<uuid:pk>/assigner/", AssignerLivreurView.as_view(), name="livraison-assigner"),
    path("<uuid:pk>/abandonner/", AbandonnerLivraisonView.as_view(), name="livraison-abandonner"),
    path("<uuid:pk>/contestation/resoudre/", ResoudreContestationView.as_view(), name="livraison-contestation-resoudre"),
]
