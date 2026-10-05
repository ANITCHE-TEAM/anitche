from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError
from django.conf import settings
import logging

from apps.core.exceptions import ErreurMetier
from apps.core.reseau import adresse_ip_client
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenRefreshSerializer

from config.schema import FICHIER, JetonsSerializer, MessageSerializer, erreurs
from .permissions import EmailVerifie

logger_securite = logging.getLogger('securite')



from .tasks import envoyer_code_otp_email, envoyer_notification_connexion
from .services import (
    resoudre_utilisateur_google,
    revoquer_tokens_actifs,
    InfosGoogleIncompletes,
    CompteDesactive,
    LiaisonGoogleRefusee,
)


from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied, ValidationError

from .models import (
    Utilisateur,
    CodeOTP,
    TypeUsageOTP,
    DocumentKYC,
    Role,
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

    La création seule ne prouve jamais que le
    demandeur possède réellement l'adresse email fournie (contrairement
    aux flux de changement d'email/téléphone ou de mot de passe oublié,
    qui exigent tous une preuve par OTP). Un compte peut donc être créé
    avec l'email de quelqu'un d'autre, mais reste marqué
    `email_verifie=False` tant que le code envoyé sur cette adresse n'a
    pas été confirmé via VerificationOTPView (même mécanisme que les
    autres flux OTP de ce module) — et c'est justement sur cet état non
    vérifié que s'appuie la connexion Google pour refuser toute liaison
    ultérieure sur ce compte (anti pré-hijacking).
    """

    queryset = Utilisateur.objects.all()
    serializer_class = InscriptionSerializer
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'inscription'

    def perform_create(self, serializer):
        utilisateur = serializer.save()

        _, code = CodeOTP.generer(utilisateur, TypeUsageOTP.INSCRIPTION)
        envoyer_code_otp_email.delay(utilisateur.email, code, TypeUsageOTP.INSCRIPTION)


@extend_schema(
    summary="Connexion (email + mot de passe)",
    responses={200: TokenObtainPairSerializer, **erreurs(401)},
)
class LoginThrottleView(TokenObtainPairView):
    """Connexion JWT avec limite de fréquence (anti brute-force)."""
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'login'  # taux défini dans DEFAULT_THROTTLE_RATES

    def post(self, request, *args, **kwargs):
        """Notifie le titulaire du compte à chaque connexion réussie,
        uniquement en cas de succès (jamais sur un
        échec, pour ne pas alerter à tort sur une simple faute de frappe
        de mot de passe ni révéler qu'un email existe)."""
        response = super().post(request, *args, **kwargs)

        if response.status_code == status.HTTP_200_OK:
            email = request.data.get('email')
            utilisateur = Utilisateur.objects.filter(email__iexact=email).first()
            if utilisateur:
                envoyer_notification_connexion.delay(
                    utilisateur.email,
                    adresse_ip_client(request) or 'inconnue',
                    request.META.get('HTTP_USER_AGENT', 'inconnu'),
                )

        return response


@extend_schema(
    summary="Rafraîchir le jeton d'accès",
    description="Renvoie un nouvel `access` et un nouveau `refresh` (rotation) ; l'ancien refresh devient inutilisable.",
    responses={200: TokenRefreshSerializer, **erreurs(401)},
)
class RafraichissementView(TokenRefreshView):
    """Rafraîchissement du jeton (simplejwt) avec une limite dédiée.

    Sans authentification, la limite porte sur l'IP : avec un access token
    de 15 minutes, chaque session active rafraîchit ~4 fois par heure, et
    derrière le CGNAT des opérateurs mobiles de nombreux utilisateurs
    partagent une IP. Le taux 'anon' global (partagé avec toute l'API) était
    épuisé par une dizaine d'utilisateurs. Aucun risque de force brute (le
    refresh token est signé) : la limite borne les écritures en base
    (rotation et liste noire).
    """

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'rafraichissement'


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
# VÉRIFICATION DU JETON PAR LE SERVICE FASTAPI (ROUTE INTERNE)
# =====================================================

@extend_schema(exclude=True)
class VerificationJetonServiceView(APIView):
    """
    Route interne, appelée par le service FastAPI (backend-fastapi) pour
    vérifier le jeton d'accès qu'un client lui présente. FastAPI délègue
    l'authentification à Django : la révocation (CHECK_REVOKE_TOKEN) et la
    désactivation du compte restent gérées ici, sans secret de signature
    partagé.

    Distincte de profil/ pour deux raisons :
    - limite de débit dédiée : throttle_classes remplace les limites par
      défaut, donc ces appels ne consomment pas la limite 'user' du compte
      (sinon le suivi GPS épuiserait les 300/h de l'utilisateur) ;
    - minimisation : FastAPI ne reçoit que l'id et le rôle.

    Exclue du schéma OpenAPI (route interne). En production, nginx répond
    404 sur ce chemin depuis Internet sans le transmettre à Django
    (infra/nginx/nginx.conf) ; FastAPI l'appelle directement sur le réseau
    Docker, en HTTP (SECURE_REDIRECT_EXEMPT dans prod.py).
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'service_fastapi'

    def get(self, request):
        return Response({'id': request.user.id, 'role': request.user.role})


# =====================================================
# DEMANDE DE CHANGEMENT D'EMAIL / TÉLÉPHONE
# =====================================================

@extend_schema(
    summary="Demander un changement d'email ou de téléphone",
    description="Envoie un code OTP (sur le nouvel email, ou sur l'email actuel pour un téléphone), à saisir dans `verification-otp/`.",
    request=DemandeChangementContactSerializer,
    responses={200: MessageSerializer},
)
class DemandeChangementContactView(APIView):
    """
    Génère un code OTP permettant de confirmer
    un changement d'adresse email ou de téléphone.
    """

    permission_classes = [IsAuthenticated, EmailVerifie]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp_envoi'

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
            # Aucun fournisseur SMS branché : le code part sur l'email
            # courant et vérifié du compte. Il prouve que le titulaire du
            # compte demande le changement, PAS qu'il possède le numéro —
            # d'où telephone_verifie laissé à False (VerificationOTPView).
            envoyer_code_otp_email.delay(request.user.email, code, type_usage)

        return Response(
            {"message": "Code envoyé."},
            status=status.HTTP_200_OK,
        )


# =====================================================
# VALIDATION DU CODE OTP
# =====================================================

@extend_schema(
    summary="Valider un code OTP",
    description="Confirme l'inscription (email vérifié) ou applique le changement de contact demandé. Code faux ou expiré : 400 `errors.code` (essais restants).",
    request=VerificationOTPSerializer,
    responses={200: MessageSerializer},
)
class VerificationOTPView(APIView):
    """
    Vérifie un code OTP puis applique
    la modification demandée.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp_verification'

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
            raise ErreurMetier("Aucun code en attente pour cette action.")

        valide, message = otp.verifier(data['code'])

        if not valide:
            raise ValidationError({"code": [message]})

        # Confirmation de l'inscription : le compte a déjà été
        # créé (email_verifie=False par défaut) ; on ne le marque vérifié
        # qu'une fois l'OTP envoyé sur cette adresse confirmé.
        if otp.type_usage == TypeUsageOTP.INSCRIPTION:
            request.user.email_verifie = True
            request.user.save(update_fields=['email_verifie'])

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

        # Mise à jour du téléphone après validation. Décision d'équipe :
        # telephone_verifie reste False tant que le code n'est pas envoyé
        # par SMS au numéro lui-même (il part aujourd'hui sur l'email).
        elif (
            otp.type_usage == 'changement_telephone'
            and otp.nouvelle_valeur
        ):
            request.user.telephone = otp.nouvelle_valeur
            request.user.telephone_verifie = False

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
# RENVOI DU CODE D'INSCRIPTION
# =====================================================

@extend_schema(
    summary="Renvoyer le code de vérification de l'email",
    description="400 si l'email est déjà vérifié.",
    request=None,
    responses={200: MessageSerializer},
)
class RenvoyerCodeInscriptionView(APIView):
    """
    Envoie un nouveau code de vérification de l'email du compte.

    Sans cet endpoint, un code d'inscription expiré (10 minutes) ou perdu
    rendait l'email invérifiable pour toujours — et donc, avec EmailVerifie,
    les actions sensibles inaccessibles. Même compteur que les autres
    envois de code (otp_envoi).
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp_envoi'

    def post(self, request):
        if request.user.email_verifie:
            raise ErreurMetier("Votre adresse email est déjà vérifiée.")

        _, code = CodeOTP.generer(request.user, TypeUsageOTP.INSCRIPTION)
        envoyer_code_otp_email.delay(request.user.email, code, TypeUsageOTP.INSCRIPTION)

        return Response(
            {"message": "Code envoyé."},
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
    # Le dépôt vaut demande vendeur : email vérifié obligatoire.
    permission_classes = [IsAuthenticated, EmailVerifie]
    throttle_scope = 'kyc'

    # Autorise l'envoi de fichiers.
    parser_classes = [
        MultiPartParser,
        FormParser,
    ]


@extend_schema(
    summary="Télécharger une pièce du dossier KYC",
    description="Titulaire du dossier ou administration. `champ` : piece_identite_recto, piece_identite_verso ou selfie.",
    responses={200: FICHIER, **erreurs(403)},
)
class TelechargerDocumentKYCView(APIView):
    """
    Sert un document KYC (pièce d'identité ou selfie) après vérification
    des droits d'accès.

    SÉCURITÉ CRITIQUE (Broken Access Control) : ces documents contiennent
    des données personnelles sensibles (pièce d'identité, photo de visage).
    Servis directement via leur URL MEDIA_URL, ils seraient accessibles
    sans aucune authentification — n'importe qui connaissant ou devinant
    le chemin du fichier pourrait le consulter. Cette vue est le SEUL
    point d'accès légitime : le champ FileField reste techniquement dans
    MEDIA_ROOT, mais son URL brute ne doit jamais être communiquée au
    frontend (voir DocumentKYCSerializer).

    Accès autorisé :
    - le propriétaire du dossier KYC (son propre document) ;
    - un admin / super_admin, pour l'instruction de la demande vendeur.
    """

    permission_classes = [IsAuthenticated]

    CHAMPS_AUTORISES = {"piece_identite_recto", "piece_identite_verso", "selfie"}

    def get(self, request, utilisateur_id, champ):
        if champ not in self.CHAMPS_AUTORISES:
            raise Http404("Document demandé inconnu.")

        # Authorization is checked BEFORE looking up the file: a non-owner,
        # non-admin requester always gets 403, whether or not a KYC file
        # exists. Checking afterwards returned 404 for users without a file
        # and 403 for users with one, revealing who submitted a KYC.
        est_proprietaire = utilisateur_id == request.user.id
        est_admin = request.user.role in (Role.ADMIN, Role.SUPER_ADMIN)

        if not (est_proprietaire or est_admin):
            logger_securite.warning(
                "Accès refusé à un document KYC (utilisateur_id=%s, demandeur_id=%s).",
                utilisateur_id, request.user.id,
            )
            raise PermissionDenied("Vous n'êtes pas autorisé à consulter ce document.")

        dossier = get_object_or_404(DocumentKYC, utilisateur_id=utilisateur_id)

        fichier = getattr(dossier, champ)
        if not fichier:
            raise Http404("Ce document n'a pas été fourni.")

        # Référencé en base mais absent du stockage (fichier perdu) : 404
        # explicite et trace, au lieu d'une erreur 500 (FileNotFoundError).
        try:
            contenu = fichier.open("rb")
        except FileNotFoundError:
            logger_securite.error(
                "Document KYC référencé mais absent du stockage (utilisateur_id=%s, champ=%s).",
                utilisateur_id, champ,
            )
            raise Http404("Ce document n'est plus disponible.")

        return FileResponse(contenu, filename=fichier.name.rsplit("/", 1)[-1])


# =====================================================
# MOT DE PASSE OUBLIÉ
# =====================================================

@extend_schema(
    summary="Mot de passe oublié : demander un code",
    description="Réponse identique que le compte existe ou non.",
    request=DemandeMotDePasseOublieSerializer,
    responses={200: MessageSerializer},
)
class DemandeMotDePasseOublieView(APIView):
    """
    Génère un OTP permettant
    la réinitialisation du mot de passe.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp_envoi'

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

@extend_schema(
    summary="Mot de passe oublié : définir le nouveau mot de passe",
    description="Code invalide ou expiré : 400 avec un message générique (sans révéler si le compte existe). Révoque les sessions ouvertes.",
    request=ConfirmationMotDePasseOublieSerializer,
    responses={200: MessageSerializer},
)
class ConfirmationMotDePasseOublieView(APIView):
    """
    Vérifie l'OTP puis définit
    un nouveau mot de passe.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'otp_verification'

    def post(self, request):

        serializer = ConfirmationMotDePasseOublieSerializer(
            data=request.data
        )

        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data

        # SÉCURITÉ (énumération de comptes) : le message d'erreur ne doit
        # JAMAIS varier selon que l'email existe ou non, ni selon qu'un
        # OTP est en attente ou non — sinon on réintroduit exactement la
        # fuite que DemandeMotDePasseOublieView évite volontairement plus
        # haut ("Si ce compte existe, un code a été envoyé."). Avec deux
        # messages distincts (email inconnu / aucun code en attente), un
        # attaquant pourrait deviner quels emails sont inscrits en
        # soumettant directement l'étape 2 avec des adresses au hasard,
        # sans jamais passer par l'étape 1.
        message_generique = "Code invalide ou expiré."

        utilisateur = Utilisateur.objects.filter(
            email__iexact=data['email']
        ).first()

        if not utilisateur:
            raise ErreurMetier(message_generique)

        # Récupère le dernier OTP valide.
        otp = CodeOTP.objects.filter(
            utilisateur=utilisateur,
            type_usage='mdp_oublie',
            utilise=False,
        ).order_by('-date_creation').first()

        if not otp:
            raise ErreurMetier(message_generique)

        valide, _ = otp.verifier(data['code'])

        if not valide:
            raise ErreurMetier(message_generique)

        # Le mot de passe est automatiquement haché.
        utilisateur.set_password(data['nouveau_password'])
        utilisateur.save(update_fields=['password'])

        # SÉCURITÉ : un reset de mot de passe répond au scénario d'un
        # compte potentiellement compromis. Sans ceci, un refresh token
        # déjà émis (à un attaquant ou sur un appareil perdu) resterait
        # valable jusqu'à 7 jours après le changement de mot de passe.
        revoquer_tokens_actifs(utilisateur)

        return Response(
            {"message": "Mot de passe réinitialisé."},
            status=status.HTTP_200_OK,
        )



@extend_schema(
    summary="Connexion avec Google",
    request=ConnexionGoogleSerializer,
    responses={200: JetonsSerializer, **erreurs(403, 409)},
)
class ConnexionGoogleView(APIView):
    """
    Connexion/inscription via Google Identity Services.

    La résolution du compte (création, liaison, garde-fous anti-
    pré-hijacking et compte désactivé) est déléguée à
    apps.utilisateurs.services.resoudre_utilisateur_google : cette vue
    se limite à l'orchestration HTTP (vérification du token Google,
    traduction des erreurs métier en réponses, émission du JWT).
    """

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
            raise ValidationError({"id_token": ["Token Google invalide."]})

        try:
            utilisateur = resoudre_utilisateur_google(infos)
        except InfosGoogleIncompletes as erreur:
            raise ErreurMetier(str(erreur))
        except CompteDesactive:
            raise PermissionDenied("Ce compte a été désactivé.")
        except LiaisonGoogleRefusee:
            raise ErreurMetier(
                "Un compte existe déjà avec cet email mais n'a "
                "pas été vérifié. Réinitialisez le mot de passe "
                "de ce compte ou contactez le support avant de "
                "vous connecter avec Google.",
                status.HTTP_409_CONFLICT,
            )

        refresh = RefreshToken.for_user(utilisateur)

        # Même notification de connexion que le
        # flux classique, uniquement en cas de succès.
        envoyer_notification_connexion.delay(
            utilisateur.email,
            adresse_ip_client(request) or 'inconnue',
            request.META.get('HTTP_USER_AGENT', 'inconnu'),
        )

        return Response(
            {
                "access": str(refresh.access_token),
                "refresh": str(refresh),
            },
            status=status.HTTP_200_OK,
        )


# =====================================================
# DÉCONNEXION
# =====================================================

@extend_schema(
    summary="Déconnexion (révocation du refresh token)",
    auth=[],
    request=inline_serializer("DeconnexionRequest", {"refresh": serializers.CharField()}),
    responses={205: OpenApiResponse(description="Refresh token révoqué, aucun corps.")},
)
class LogoutView(APIView):
    """
    Déconnecte l'utilisateur en révoquant son refresh token.

    Sans ceci, le token_blacklist installé (SIMPLE_JWT +
    rest_framework_simplejwt.token_blacklist) n'avait aucun point
    d'entrée : un refresh token restait valable jusqu'à son expiration
    naturelle (7 jours) même après une déconnexion explicite.

    Même modèle que rest_framework_simplejwt.views.TokenBlacklistView
    (et que LoginThrottleView / TokenRefreshView déjà utilisées dans ce
    module) : authentication_classes = () et permission_classes =
    [AllowAny]. Posséder le refresh token EST la preuve d'autorisation ;
    aucune vérification de propriété via un access token n'est donc
    nécessaire ni souhaitable ici. Exiger IsAuthenticated obligerait le
    frontend à rafraîchir un access token expiré avant de pouvoir se
    déconnecter — et un Authorization header expiré/invalide envoyé en
    plus du corps ferait de toute façon échouer JWTAuthentication avant
    même d'atteindre la vue, malgré permission_classes.

    Un scope de throttle dédié (et non le seul throttle 'anon' générique
    déjà actif) : sans authentification ni CSRF, cette vue reste un
    point d'entrée public qui provoque une écriture en base
    (BlacklistedToken) à chaque appel — une limite basse et spécifique
    évite qu'elle serve de vecteur de spam/déni de service low-cost.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'logout'

    def post(self, request):
        refresh_token = request.data.get('refresh')

        if not refresh_token:
            raise ValidationError({"refresh": ["Le refresh token est requis."]})

        try:
            token = RefreshToken(refresh_token)
        except TokenError:
            raise ValidationError({"refresh": ["Token invalide ou déjà expiré."]})

        token.blacklist()

        return Response(status=status.HTTP_205_RESET_CONTENT)