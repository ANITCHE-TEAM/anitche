from django.urls import path
from .views import (
    DemandeRetourListCreateView,
    DemandeRetourDetailView,
    AjouterPhotoRetourView,
    TraiterDemandeRetourView,
    EspaceVendeurRetoursListView,
)

app_name = "retours"

urlpatterns = [
    path("", DemandeRetourListCreateView.as_view(), name="retour-liste-creer"),
    path("<uuid:pk>/", DemandeRetourDetailView.as_view(), name="retour-detail"),
    path("<uuid:pk>/photos/", AjouterPhotoRetourView.as_view(), name="retour-ajouter-photo"),
    path("<uuid:pk>/traiter/", TraiterDemandeRetourView.as_view(), name="retour-traiter"),
    path("vendeur/liste/", EspaceVendeurRetoursListView.as_view(), name="vendeur-retours-liste"),
]
