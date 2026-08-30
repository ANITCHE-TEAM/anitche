from django.urls import path
from .views import (
    InitierPaiementView,
    PaiementListView,
    PaiementDetailView,
    WebhookPaiementView,
)

app_name = "paiements"

urlpatterns = [
    path("initier/", InitierPaiementView.as_view(), name="initier-paiement"),
    path("", PaiementListView.as_view(), name="paiement-liste"),
    path("<uuid:pk>/", PaiementDetailView.as_view(), name="paiement-detail"),
    path("webhook/<str:fournisseur>/", WebhookPaiementView.as_view(), name="webhook-paiement"),
]
