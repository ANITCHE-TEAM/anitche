from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Utilisateur, CodeOTP, DocumentKYC, StatutsKYCImpossibles
from .serializers import (
    InscriptionSerializer,
    ProfilSerializer,
    DemandeChangementContactSerializer,
    VerificationOTPSerializer,
    DocumentKYCSerializer,
    DemandeMotDePasseOublieSerializer,
    ConfirmationMotDePasseOublieSerializer
)
from rest_framework.parsers import MultiPartParser, FormParser

class InscriptionView(generics.CreateAPIView):
    queryset = Utilisateur.objects.all()
    serializer_class = InscriptionSerializer
    permission_classes = [AllowAny]


class ProfilView(generics.RetrieveUpdateAPIView):
    serializer_class = ProfilSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        return self.request.user


class DemandeChangementContactView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = DemandeChangementContactSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data.get('nouvel_email'):
            type_usage = 'changement_email'
            nouvelle_valeur = data['nouvel_email']
        else:
            type_usage = 'changement_telephone'
            nouvelle_valeur = data['nouveau_telephone']

        _, code = CodeOTP.generer(request.user, type_usage, nouvelle_valeur)

        # TODO : brancher l'envoi réel du code par email/SMS ici (Celery + provider SMS/email)
        # print(f"[DEBUG] Code OTP pour {request.user.email}: {code}")

        return Response(
            {"message": "Code envoyé."},
            status=status.HTTP_200_OK,
)
    
class VerificationOTPView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = VerificationOTPSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

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
            return Response({"message": message}, status=status.HTTP_400_BAD_REQUEST)

        if otp.type_usage == 'changement_email' and otp.nouvelle_valeur:
            request.user.email = otp.nouvelle_valeur
            request.user.email_verifie = True
            request.user.save(update_fields=['email', 'email_verifie'])

        elif otp.type_usage == 'changement_telephone' and otp.nouvelle_valeur:
            request.user.telephone = otp.nouvelle_valeur
            request.user.telephone_verifie = True
            request.user.save(update_fields=['telephone', 'telephone_verifie'])

        return Response({"message": message}, status=status.HTTP_200_OK)


class DemandeVendeurView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not hasattr(request.user, 'dossier_kyc'):
            return Response(
                {"message": "Vous devez d'abord soumettre vos documents KYC."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            request.user.soumettre_demande_vendeur()
        except StatutsKYCImpossibles as erreur:
            return Response({"message": str(erreur)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {"message": "Demande vendeur enregistrée, en attente de validation."},
            status=status.HTTP_200_OK,
        )


class UploadKYCView(generics.CreateAPIView):
    serializer_class = DocumentKYCSerializer
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]


class DemandeMotDePasseOublieView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = DemandeMotDePasseOublieSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email']

        utilisateur = Utilisateur.objects.filter(email__iexact=email).first()
        if utilisateur:
            _, code = CodeOTP.generer(utilisateur, 'mdp_oublie')
            # TODO : envoi réel du code par email
            # print(f"[DEBUG] Code reset pour {email}: {code}")

        # Toujours la même réponse, qu'un compte existe ou non
        return Response(
            {"message": "Si ce compte existe, un code a été envoyé."},
            status=status.HTTP_200_OK,
        )


class ConfirmationMotDePasseOublieView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = ConfirmationMotDePasseOublieSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        utilisateur = Utilisateur.objects.filter(email__iexact=data['email']).first()
        if not utilisateur:
            return Response({"message": "Code invalide."}, status=status.HTTP_400_BAD_REQUEST)

        otp = CodeOTP.objects.filter(
            utilisateur=utilisateur, type_usage='mdp_oublie', utilise=False,
        ).order_by('-date_creation').first()

        if not otp:
            return Response({"message": "Aucun code en attente."}, status=status.HTTP_400_BAD_REQUEST)

        valide, message = otp.verifier(data['code'])
        if not valide:
            return Response({"message": message}, status=status.HTTP_400_BAD_REQUEST)

        utilisateur.set_password(data['nouveau_password'])
        utilisateur.save(update_fields=['password'])

        return Response({"message": "Mot de passe réinitialisé."}, status=status.HTTP_200_OK)