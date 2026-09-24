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

# Transitions valides pour un livreur (hors admin, qui garde la main pour
# une correction manuelle exceptionnelle). Sans cette table, un livreur
# pouvait sauter des étapes (EN_ATTENTE -> LIVREE directement) ou faire
# régresser un statut (EN_COURS -> EN_ATTENTE) librement (A04:2025 —
# Insecure Design : aucune validation de la machine à états).
TRANSITIONS_LIVRAISON_VALIDES = {
    Livraison.Status.EN_ATTENTE: {Livraison.Status.EXPEDIEE},
    Livraison.Status.EXPEDIEE: {Livraison.Status.EN_COURS, Livraison.Status.ECHOUEE},
    Livraison.Status.EN_COURS: {Livraison.Status.LIVREE, Livraison.Status.ECHOUEE},
    Livraison.Status.LIVREE: set(),
    Livraison.Status.ECHOUEE: set(),
}


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
    """Détail d'une livraison — accès réservé au client concerné, au livreur
    assigné, ou à un admin (même isolation que LivraisonListView).
    """
    serializer_class = LivraisonSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "pk"

    def get_queryset(self):
        user = self.request.user
        qs = Livraison.objects.select_related("commande", "livreur")

        if user.role in (Role.ADMIN, Role.SUPER_ADMIN):
            return qs
        if user.role == Role.LIVREUR:
            return qs.filter(livreur=user)
        return qs.filter(commande__client=user)


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

        # États terminaux : une fois livrée ou définitivement échouée, seul
        # un admin peut encore la modifier (correction manuelle exceptionnelle).
        # Sans cette garde, un livreur pouvait faire régresser une livraison
        # déjà "livrée" vers un état antérieur, ou modifier son statut après
        # échec, sans trace justifiée de cette anomalie.
        etats_terminaux = (Livraison.Status.LIVREE, Livraison.Status.ECHOUEE)
        if livraison.status in etats_terminaux and not est_admin:
            return Response(
                {"detail": "Cette livraison est dans un état final et ne peut plus être modifiée par un livreur."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = LivraisonChangerStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        nouveau_status = serializer.validated_data["status"]

        # Un admin garde la main pour une correction manuelle exceptionnelle ;
        # un livreur doit suivre l'ordre normal des étapes.
        if not est_admin:
            transitions_autorisees = TRANSITIONS_LIVRAISON_VALIDES.get(livraison.status, set())
            if nouveau_status not in transitions_autorisees:
                return Response(
                    {
                        "detail": (
                            f"Transition invalide : impossible de passer de "
                            f"'{livraison.status}' à '{nouveau_status}'."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        livraison.changer_status(
            nouveau_status=nouveau_status,
            effectue_par=user,
            commentaire=serializer.validated_data.get("commentaire", ""),
        )

        return Response(LivraisonSerializer(livraison).data, status=status.HTTP_200_OK)


class LivraisonHistoriqueListView(generics.ListAPIView):
    """Historique d'une livraison — même règle d'accès que le détail : seul
    le client concerné, le livreur assigné, ou un admin peuvent le consulter.
    """
    serializer_class = LivraisonHistoriqueSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        livraison = get_object_or_404(Livraison, pk=self.kwargs["livraison_id"])

        est_client = livraison.commande.client_id == user.id
        est_livreur_assigne = livraison.livreur_id == user.id
        est_admin = user.role in (Role.ADMIN, Role.SUPER_ADMIN)

        if not (est_client or est_livreur_assigne or est_admin):
            return LivraisonHistorique.objects.none()

        return LivraisonHistorique.objects.filter(livraison_id=self.kwargs["livraison_id"])