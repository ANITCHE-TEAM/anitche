from django.urls import path

from .views import (
    ArticlesANoterListView,
    AvisCreerView,
    AvisDetailView,
    AvisRecusListView,
    MasquerAvisView,
    MesAvisListView,
    ProduitAvisListView,
    RejeterSignalementView,
    ReponseAvisView,
    RetablirAvisView,
    SignalementsModerationListView,
    SignalerAvisView,
)

app_name = "avis"

urlpatterns = [
    path("", AvisCreerView.as_view(), name="avis-creer"),
    path("a-noter/", ArticlesANoterListView.as_view(), name="avis-a-noter"),
    path("mes-avis/", MesAvisListView.as_view(), name="avis-mes-avis"),
    path("recus/", AvisRecusListView.as_view(), name="avis-recus"),
    path("produit/<int:produit_id>/", ProduitAvisListView.as_view(), name="avis-produit"),
    path("moderation/signalements/", SignalementsModerationListView.as_view(), name="avis-moderation-signalements"),
    path(
        "moderation/signalements/<int:pk>/rejeter/",
        RejeterSignalementView.as_view(),
        name="avis-moderation-rejeter",
    ),
    path("moderation/avis/<uuid:pk>/masquer/", MasquerAvisView.as_view(), name="avis-moderation-masquer"),
    path("moderation/avis/<uuid:pk>/retablir/", RetablirAvisView.as_view(), name="avis-moderation-retablir"),
    path("<uuid:pk>/", AvisDetailView.as_view(), name="avis-detail"),
    path("<uuid:pk>/reponse/", ReponseAvisView.as_view(), name="avis-reponse"),
    path("<uuid:pk>/signaler/", SignalerAvisView.as_view(), name="avis-signaler"),
]
