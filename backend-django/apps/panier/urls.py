from django.urls import path
from .views import (
    PanierDetailView,
    PanierItemListCreateView,
    PanierItemDetailView,
)

app_name = "panier"

urlpatterns = [
    path("panier/", PanierDetailView.as_view(), name="panier-detail"),
    path("panier/items/", PanierItemListCreateView.as_view(), name="panier-items"),
    path("panier/items/<uuid:pk>/", PanierItemDetailView.as_view(), name="panier-item-detail"),
]