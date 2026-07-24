from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import (
    InscriptionView,
    ProfilView,
    DemandeChangementContactView,
    VerificationOTPView,
    DemandeVendeurView,
    UploadKYCView,
    DemandeMotDePasseOublieView,
    ConfirmationMotDePasseOublieView
)

urlpatterns = [
    path('inscription/', InscriptionView.as_view(), name='inscription'),
    path('connexion/', TokenObtainPairView.as_view(), name='connexion'),
    path('connexion/rafraichir/', TokenRefreshView.as_view(), name='connexion-refresh'),
    path('profil/', ProfilView.as_view(), name='profil'),
    path('changement-contact/', DemandeChangementContactView.as_view(), name='changement-contact'),
    path('verification-otp/', VerificationOTPView.as_view(), name='verification-otp'),
    path('demande-vendeur/', DemandeVendeurView.as_view(), name='demande-vendeur'),
    path('upload-kyc/', UploadKYCView.as_view(), name='upload-kyc'),
     path('mot-de-passe-oublie/', DemandeMotDePasseOublieView.as_view(), name='mdp-oublie'),
    path('mot-de-passe-oublie/confirmer/', ConfirmationMotDePasseOublieView.as_view(), name='mdp-oublie-confirmer'),
]