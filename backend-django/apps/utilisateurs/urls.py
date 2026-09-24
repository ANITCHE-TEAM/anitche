from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import (
    InscriptionView,
    LoginThrottleView,
    ProfilView,
    DemandeChangementContactView,
    VerificationOTPView,
    DemandeVendeurView,
    UploadKYCView,
    TelechargerDocumentKYCView,
    DemandeMotDePasseOublieView,
    ConfirmationMotDePasseOublieView,
    ConnexionGoogleView,
    LogoutView
)

# =====================================================
# ROUTES DE L'APPLICATION UTILISATEURS
# =====================================================

urlpatterns = [

    # Création d'un nouveau compte utilisateur.
    path(
        'inscription/',
        InscriptionView.as_view(),
        name='inscription'
    ),

    # Authentification avec JWT.
    path(
        'connexion/',
        LoginThrottleView.as_view(),
        name='connexion'
    ),

    # Génération d'un nouveau jeton d'accès
    # à partir d'un refresh token valide.
    path(
        'connexion/rafraichir/',
        TokenRefreshView.as_view(),
        name='connexion-refresh'
    ),

    # Consultation du profil de l'utilisateur connecté.
    path(
        'profil/',
        ProfilView.as_view(),
        name='profil'
    ),

    # Demande de changement d'email ou de téléphone.
    path(
        'changement-contact/',
        DemandeChangementContactView.as_view(),
        name='changement-contact'
    ),

    # Validation d'un code OTP.
    path(
        'verification-otp/',
        VerificationOTPView.as_view(),
        name='verification-otp'
    ),

    # Demande d'accès au statut de vendeur.
    path(
        'demande-vendeur/',
        DemandeVendeurView.as_view(),
        name='demande-vendeur'
    ),

    # Envoi des documents KYC.
    path(
        'upload-kyc/',
        UploadKYCView.as_view(),
        name='upload-kyc'
    ),

    # Téléchargement contrôlé d'un document KYC (propriétaire ou admin
    # uniquement) — remplace l'accès direct via MEDIA_URL.
    path(
        'kyc/<int:utilisateur_id>/<str:champ>/',
        TelechargerDocumentKYCView.as_view(),
        name='kyc-telecharger'
    ),

    # Première étape de la réinitialisation
    # du mot de passe : demande d'un OTP.
    path(
        'mot-de-passe-oublie/',
        DemandeMotDePasseOublieView.as_view(),
        name='mdp-oublie'
    ),

    # Deuxième étape : vérification de l'OTP
    # puis définition d'un nouveau mot de passe.
    path(
        'mot-de-passe-oublie/confirmer/',
        ConfirmationMotDePasseOublieView.as_view(),
        name='mdp-oublie-confirmer'
    ),
    # Route vers connexkon via compte google
    path('connexion-google/', ConnexionGoogleView.as_view(), name='connexion-google'),

    # Déconnexion : révoque le refresh token fourni.
    path('deconnexion/', LogoutView.as_view(), name='deconnexion'),
]