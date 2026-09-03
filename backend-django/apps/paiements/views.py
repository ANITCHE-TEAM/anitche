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
    """Point d'entrée pour les webhooks et notifications asynchrones des passerelles de paiement."""

    permission_classes = [AllowAny]

    def post(self, request, fournisseur):
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
