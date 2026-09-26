from django.urls import path
from .views import (
    PasseportPublicVerificationView,
    PasseportVendeurListCreateView,
    PasseportVendeurDetailView,
)

app_name = "passeport_qr"

urlpatterns = [
    path("verifier/<str:code_passeport>/", PasseportPublicVerificationView.as_view(), name="passeport-public-verification"),
    path("vendeur/", PasseportVendeurListCreateView.as_view(), name="passeport-vendeur-liste-creer"),
    path("vendeur/<uuid:pk>/", PasseportVendeurDetailView.as_view(), name="passeport-vendeur-detail"),
]
