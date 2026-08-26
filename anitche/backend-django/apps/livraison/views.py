from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.utilisateurs.models import Role
from .models import Livraison, LivraisonHistorique
from .serializers import (
    LivraisonSerializer,
    LivraisonHistoriqueSerializer,
    LivraisonChangerStatusSerializer,
)


class LivraisonListView(generics.ListAPIView):
    """Liste des livraisons.

    - Client  : uniquement ses propres livraisons.
    - Livreur : uniquement les livraisons qui lui sont assignées.
    - Admin / Super admin : toutes les livraisons.
    """
    serializer_class = LivraisonSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = Livraison.objects.select_related("commande", "livreur")

        if user.role in (Role.ADMIN, Role.SUPER_ADMIN):
            return qs
        if user.role == Role.LIVREUR:
            return qs.filter(livreur=user)
        return qs.filter(commande__client=user)


class LivraisonDetailView(generics.RetrieveAPIView):
    serializer_class = LivraisonSerializer
    permission_classes = [IsAuthenticated]
    queryset = Livraison.objects.select_related("commande", "livreur")
    lookup_field = "pk"


class LivraisonChangerStatusView(APIView):
    """Permet à un livreur assigné (ou un admin) de faire progresser
    le statut d'une livraison. L'historique est mis à jour automatiquement.
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        livraison = get_object_or_404(Livraison, pk=pk)
        user = request.user

        est_livreur_assigne = livraison.livreur_id == user.id
        est_admin = user.role in (Role.ADMIN, Role.SUPER_ADMIN)

        if not (est_livreur_assigne or est_admin):
            return Response(
                {"detail": "Vous n'êtes pas autorisé à modifier cette livraison."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = LivraisonChangerStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        livraison.changer_status(
            nouveau_status=serializer.validated_data["status"],
            effectue_par=user,
            commentaire=serializer.validated_data.get("commentaire", ""),
        )

        return Response(LivraisonSerializer(livraison).data, status=status.HTTP_200_OK)


class LivraisonHistoriqueListView(generics.ListAPIView):
    serializer_class = LivraisonHistoriqueSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return LivraisonHistorique.objects.filter(
            livraison_id=self.kwargs["livraison_id"]
        )