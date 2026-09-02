from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from rest_framework_simplejwt.tokens import RefreshToken
from django.conf import settings
import logging

logger_securite = logging.getLogger('securite')



from .tasks import envoyer_code_otp_email


from .models import (
    Utilisateur,
    CodeOTP,
    StatutsKYCImpossibles,
)

from .serializers import (
    InscriptionSerializer,
    ProfilSerializer,
    DemandeChangementContactSerializer,
    VerificationOTPSerializer,
    DocumentKYCSerializer,
    DemandeMotDePasseOublieSerializer,
    ConfirmationMotDePasseOublieSerializer,
    ConnexionGoogleSerializer
)


# =====================================================
# INSCRIPTION
# =====================================================

class InscriptionView(generics.CreateAPIView):
    """
    Permet à un visiteur de créer un nouveau compte.
    """

    queryset = Utilisateur.objects.all()
    serializer_class = InscriptionSerializer
    permission_classes = [AllowAny]


class LoginThrottleView(TokenObtainPairView):
    """Connexion JWT avec limite de fréquence (anti brute-force)."""
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'  # taux défini dans DEFAULT_THROTTLE_RATES


# =====================================================
# PROFIL UTILISATEUR
# =====================================================

class ProfilView(generics.RetrieveUpdateAPIView):
    """
    Consultation et modification du profil
    de l'utilisateur authentifié.
    """

    serializer_class = ProfilSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        # L'utilisateur ne peut accéder qu'à son propre profil.
        return self.request.user


# =====================================================
# DEMANDE DE CHANGEMENT D'EMAIL / TÉLÉPHONE
# =====================================================

class DemandeChangementContactView(APIView):
    """
    Génère un code OTP permettant de confirmer
    un changement d'adresse email ou de téléphone.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp'

    def post(self, request):
        serializer = DemandeChangementContactSerializer(
            data=request.data,
            context={'request': request},
        )
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data

        # Détermine le type de modification demandée.
        if data.get('nouvel_email'):
            type_usage = 'changement_email'
            nouvelle_valeur = data['nouvel_email']
        else:
            type_usage = 'changement_telephone'
            nouvelle_valeur = data['nouveau_telephone']

        # Génération d'un nouveau code OTP.
        _, code = CodeOTP.generer(request.user, type_usage, nouvelle_valeur)

        # Le code doit être envoyé sur le canal que l'on cherche à
        # vérifier, jamais sur l'ancien. Sinon l'OTP ne prouve jamais
        # que l'utilisateur possède réellement la nouvelle adresse, et
        # n'importe qui peut s'approprier l'email d'un tiers.
        if type_usage == 'changement_email':
            envoyer_code_otp_email.delay(nouvelle_valeur, code, type_usage)
        else:
            # TODO : brancher un fournisseur SMS. En attendant, le code
            # part sur l'email courant et vérifié du compte — c'est un
            # canal déjà prouvé, contrairement au nouveau numéro.
            envoyer_code_otp_email.delay(request.user.email, code, type_usage)

        return Response(
            {"message": "Code envoyé."},
            status=status.HTTP_200_OK,
        )


# =====================================================
# VALIDATION DU CODE OTP
# =====================================================

class VerificationOTPView(APIView):
    """
    Vérifie un code OTP puis applique
    la modification demandée.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp'

    def post(self, request):
        serializer = VerificationOTPSerializer(
            data=request.data
        )

        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data

        # Récupère le dernier OTP non utilisé.
        otp = CodeOTP.objects.filter(
            utilisateur=request.user,
            type_usage=data['type_usage'],
            utilise=False,
        ).order_by('-date_creation').first()

        if not otp:
            return Response(
                {"message": "Aucun code en attente pour cette action."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        valide, message = otp.verifier(data['code'])

        if not valide:
            return Response(
                {"message": message},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Mise à jour de l'email après validation.
        if (
            otp.type_usage == 'changement_email'
            and otp.nouvelle_valeur
        ):
            request.user.email = otp.nouvelle_valeur
            request.user.email_verifie = True

            request.user.save(
                update_fields=[
                    'email',
                    'email_verifie',
                ]
            )

        # Mise à jour du téléphone après validation.
        elif (
            otp.type_usage == 'changement_telephone'
            and otp.nouvelle_valeur
        ):
            request.user.telephone = otp.nouvelle_valeur
            request.user.telephone_verifie = True

            request.user.save(
                update_fields=[
                    'telephone',
                    'telephone_verifie',
                ]
            )

        return Response(
            {"message": message},
            status=status.HTTP_200_OK,
        )


# =====================================================
# DEMANDE DE STATUT VENDEUR
# =====================================================

class DemandeVendeurView(APIView):
    """
    Permet à un utilisateur de demander
    l'obtention du statut vendeur.
    """

    permission_classes = [IsAuthenticated]
    throttle_scope = 'kyc'

    def post(self, request):

        # Le dossier KYC est obligatoire.
        if not hasattr(request.user, 'dossier_kyc'):
            return Response(
                {
                    "message":
                    "Vous devez d'abord soumettre vos documents KYC."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            request.user.soumettre_demande_vendeur()

        except StatutsKYCImpossibles as erreur:
            return Response(
                {"message": str(erreur)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "message":
                "Demande vendeur enregistrée, en attente de validation."
            },
            status=status.HTTP_200_OK,
        )


# =====================================================
# ENVOI DU DOSSIER KYC
# =====================================================

class UploadKYCView(generics.CreateAPIView):
    """
    Permet l'envoi des documents nécessaires
    à la vérification d'identité.
    """

    serializer_class = DocumentKYCSerializer
    permission_classes = [IsAuthenticated]
    throttle_scope = 'kyc'

    # Autorise l'envoi de fichiers.
    parser_classes = [
        MultiPartParser,
        FormParser,
    ]


# =====================================================
# MOT DE PASSE OUBLIÉ
# =====================================================

class DemandeMotDePasseOublieView(APIView):
    """
    Génère un OTP permettant
    la réinitialisation du mot de passe.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp'

    def post(self, request):

        serializer = DemandeMotDePasseOublieSerializer(
            data=request.data
        )

        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data['email']

        utilisateur = Utilisateur.objects.filter(
            email__iexact=email
        ).first()

        if utilisateur:
            _, code = CodeOTP.generer(utilisateur, 'mdp_oublie')
            envoyer_code_otp_email.delay(utilisateur.email, code, 'mdp_oublie')

            # TODO : envoyer le code par email.

        # Réponse identique afin de ne pas révéler
        # si un compte existe (protection contre
        # l'énumération d'emails).
        return Response(
            {
                "message":
                "Si ce compte existe, un code a été envoyé."
            },
            status=status.HTTP_200_OK,
        )


# =====================================================
# CONFIRMATION DE LA RÉINITIALISATION
# =====================================================

class ConfirmationMotDePasseOublieView(APIView):
    """
    Vérifie l'OTP puis définit
    un nouveau mot de passe.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp'

    def post(self, request):

        serializer = ConfirmationMotDePasseOublieSerializer(
            data=request.data
        )

        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data

        utilisateur = Utilisateur.objects.filter(
            email__iexact=data['email']
        ).first()

        if not utilisateur:
            return Response(
                {"message": "Code invalide."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Récupère le dernier OTP valide.
        otp = CodeOTP.objects.filter(
            utilisateur=utilisateur,
            type_usage='mdp_oublie',
            utilise=False,
        ).order_by('-date_creation').first()

        if not otp:
            return Response(
                {"message": "Aucun code en attente."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        valide, message = otp.verifier(data['code'])

        if not valide:
            return Response(
                {"message": message},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Le mot de passe est automatiquement haché.
        utilisateur.set_password(data['nouveau_password'])
        utilisateur.save(update_fields=['password'])

        return Response(
            {"message": "Mot de passe réinitialisé."},
            status=status.HTTP_200_OK,
        )



class ConnexionGoogleView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'

    def post(self, request):
        serializer = ConnexionGoogleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            infos = google_id_token.verify_oauth2_token(
                serializer.validated_data['id_token'],
                google_requests.Request(),
                settings.GOOGLE_OAUTH_CLIENT_ID,
            )
        except ValueError:
            return Response(
                {"message": "Token Google invalide."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        google_id = infos.get('sub')
        email = infos.get('email')
        email_verifie_google = infos.get('email_verified', False)

        if not google_id or not email or not email_verifie_google:
            return Response(
                {"message": "Informations Google incomplètes ou non vérifiées."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 1. Compte déjà lié à ce google_id -> connexion directe.
        utilisateur = Utilisateur.objects.filter(google_id=google_id).first()

        if not utilisateur:
            # 2. Sinon, un compte existe peut-être déjà avec cet email
            #    (inscription classique) -> on le lie à Google.
            utilisateur, cree = Utilisateur.objects.get_or_create(
                email__iexact=email,
                defaults={
                    'email': email,
                    'nom': infos.get('family_name', ''),
                    'prenom': infos.get('given_name', ''),
                    'email_verifie': True,
                    'google_id': google_id,
                },
            )

            if cree:
                utilisateur.set_unusable_password()
                utilisateur.save(update_fields=['password'])
            else:
                utilisateur.google_id = google_id
                if not utilisateur.email_verifie:
                    utilisateur.email_verifie = True
                utilisateur.save(update_fields=['google_id', 'email_verifie'])

        # Contrôle d'accès : un compte désactivé (banni, fraude, etc.)
        # ne doit jamais recevoir de token, quel que soit le moyen de
        # connexion. RefreshToken.for_user() ne fait volontairement AUCUNE
        # vérification d'is_active (contrairement à authenticate()) donc
        # ce garde-fou doit être posé explicitement ici.
        if not utilisateur.is_active:
            logger_securite.warning(
                "Tentative de connexion Google refusée : compte désactivé "
                "(utilisateur_id=%s, email=%s)",
                utilisateur.id, utilisateur.email,
            )
            return Response(
                {"message": "Ce compte a été désactivé."},
                status=status.HTTP_403_FORBIDDEN,
            )

        refresh = RefreshToken.for_user(utilisateur)

        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
            },
            status=status.HTTP_200_OK,
        )