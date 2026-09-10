import hashlib
import hmac
import logging

from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny

from .models import Paiement
from .serializers import (
    InitierPaiementSerializer,
    PaiementSerializer,
    WebhookPaiementSerializer,
)
from .services import ServicePaiement
from apps.utilisateurs.models import Role

logger_securite = logging.getLogger('securite')


class InitierPaiementView(APIView):
    """Permet à un client connecté d'initier un paiement pour sa commande ou son groupe de commandes."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = InitierPaiementSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        paiement = ServicePaiement.initier_paiement(
            client=request.user,
            validated_data=serializer.validated_data,
        )

        return Response(PaiementSerializer(paiement).data, status=status.HTTP_201_CREATED)


class PaiementListView(generics.ListAPIView):
    """Liste des paiements.

    - Client standard : uniquement ses propres paiements.
    - Admin / Staff : tous les paiements de la plateforme.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = PaiementSerializer

    def get_queryset(self):
        user = self.request.user
        qs = Paiement.objects.select_related("client", "commande", "groupe_commande")

        if user.role in (Role.ADMIN, Role.SUPER_ADMIN):
            return qs
        return qs.filter(client=user)


class PaiementDetailView(generics.RetrieveAPIView):
    """Consultation détaillée d'un paiement spécifique."""

    permission_classes = [IsAuthenticated]
    serializer_class = PaiementSerializer

    def get_queryset(self):
        user = self.request.user
        qs = Paiement.objects.select_related("client", "commande", "groupe_commande")

        if user.role in (Role.ADMIN, Role.SUPER_ADMIN):
            return qs
        return qs.filter(client=user)


class WebhookPaiementView(APIView):
    """Point d'entrée pour les webhooks et notifications asynchrones des passerelles de paiement.

    SÉCURITÉ CRITIQUE : ce endpoint est forcément `AllowAny` côté DRF (les
    passerelles de paiement n'ont pas de compte Anitche, donc pas de JWT),
    mais ça ne veut PAS dire "sans authentification" — l'authenticité de la
    requête est vérifiée par une signature HMAC-SHA256 calculée sur le corps
    brut de la requête avec un secret partagé propre à chaque fournisseur
    (jamais exposé au client, jamais dans le code, uniquement en variable
    d'environnement). Sans ce contrôle, n'importe qui pouvait simuler un
    paiement réussi pour n'importe quelle commande dont il connaît la
    référence — la référence étant elle-même renvoyée au client par
    InitierPaiementView, l'exploit était trivial pour un attaquant qui crée
    simplement son propre paiement EN_ATTENTE.

    Le nom exact de l'en-tête et l'algorithme de signature diffèrent selon
    le fournisseur réel (Wave, Orange Money, MTN...) : à adapter précisément
    à la documentation de chaque passerelle une fois l'intégration réelle
    branchée. Le principe — exiger et vérifier un secret partagé avant tout
    traitement — reste la partie non négociable.
    """

    permission_classes = [AllowAny]

    def _verifier_signature(self, request, fournisseur):
        secret = settings.WEBHOOK_SECRETS.get(fournisseur)
        if not secret:
            logger_securite.error(
                "Webhook refusé : aucun secret configuré pour le fournisseur '%s'.", fournisseur
            )
            return False

        signature_recue = request.headers.get('X-Webhook-Signature', '')
        if not signature_recue:
            logger_securite.warning(
                "Webhook refusé : signature manquante (fournisseur=%s, ip=%s).",
                fournisseur, request.META.get('REMOTE_ADDR'),
            )
            return False

        signature_attendue = hmac.new(
            secret.encode('utf-8'),
            request.body,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(signature_recue, signature_attendue):
            logger_securite.warning(
                "Webhook refusé : signature invalide (fournisseur=%s, ip=%s).",
                fournisseur, request.META.get('REMOTE_ADDR'),
            )
            return False

        return True

    def post(self, request, fournisseur):
        if not self._verifier_signature(request, fournisseur):
            return Response(
                {"erreur": "Signature invalide ou manquante."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        data = request.data.copy()
        data["fournisseur"] = fournisseur

        serializer = WebhookPaiementSerializer(data=data)
        serializer.is_valid(raise_exception=True)

        succes, message = ServicePaiement.traiter_webhook(
            fournisseur=fournisseur,
            evenement_id=serializer.validated_data["evenement_id"],
            reference=serializer.validated_data["reference"],
            statut=serializer.validated_data["statut"],
            transaction_id_externe=serializer.validated_data.get("transaction_id_externe"),
            payload=request.data,
            metadata=serializer.validated_data.get("metadata", {}),
        )

        if not succes:
            return Response({"erreur": message}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"message": message}, status=status.HTTP_200_OK)