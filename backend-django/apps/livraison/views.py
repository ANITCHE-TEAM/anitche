from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.commandes.services import TransitionImpossible
from apps.utilisateurs.models import Role, Utilisateur
from apps.vendeurs.permissions import ROLES_ADMINISTRATION, EstAdministrateur, EstVendeurValide

from . import services
from .models import ContestationLivraison, Livraison, LivraisonHistorique
from .permissions import EstLivreurOuAdministrateur
from .serializers import (
    AbandonnerLivraisonSerializer,
    AssignerLivreurSerializer,
    ContestationSerializer,
    ContesterLivraisonSerializer,
    LivraisonAdministrationSerializer,
    LivraisonAReassignerSerializer,
    LivraisonChangerStatusSerializer,
    LivraisonClientSerializer,
    LivraisonHistoriqueAdministrationSerializer,
    LivraisonHistoriqueSerializer,
    LivraisonLivreurSerializer,
    LivraisonVendeurSerializer,
    LivreurSerializer,
    NommerLivreurSerializer,
    ResoudreContestationSerializer,
)


def _base():
    return Livraison.objects.select_related("commande__groupe", "livreur", "contestation")


def livraisons_visibles(utilisateur):
    """Isolation par rôle : (queryset, serializer).

    - Administration : toutes les livraisons.
    - Livreur : uniquement celles qui lui sont assignées.
    - Tout autre compte : uniquement celles de ses propres commandes (un
      vendeur voit celles de sa boutique par /vendeur/).
    """
    if utilisateur.role in ROLES_ADMINISTRATION:
        return _base(), LivraisonAdministrationSerializer
    if utilisateur.role == Role.LIVREUR:
        return _base().filter(livreur=utilisateur), LivraisonLivreurSerializer
    return _base().filter(commande__client=utilisateur), LivraisonClientSerializer


def livraison_visible(utilisateur, pk):
    queryset, serializer = livraisons_visibles(utilisateur)
    return get_object_or_404(queryset, pk=pk), serializer


def _representation(utilisateur, livraison):
    livraison, serializer = livraison_visible(utilisateur, livraison.pk)
    return serializer(livraison).data


def _refus(erreur):
    if isinstance(erreur, TransitionImpossible):
        return Response({"detail": str(erreur)}, status=status.HTTP_409_CONFLICT)
    return Response({"detail": str(erreur)}, status=erreur.code_http)


class LivraisonListView(generics.ListAPIView):
    """Liste filtrée par rôle. Administration : filtres `?status=` et
    `?contestation=ouverte`."""
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset, _ = livraisons_visibles(self.request.user)
        if self.request.user.role in ROLES_ADMINISTRATION:
            statut = self.request.query_params.get("status")
            if statut:
                queryset = queryset.filter(status=statut)
            if self.request.query_params.get("contestation") == ContestationLivraison.Statut.OUVERTE:
                queryset = queryset.filter(contestation__statut=ContestationLivraison.Statut.OUVERTE)
        return queryset

    def get_serializer_class(self):
        return livraisons_visibles(self.request.user)[1]


class LivraisonDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        livraison, serializer = livraison_visible(request.user, pk)
        return Response(serializer(livraison).data)


class LivraisonChangerStatusView(APIView):
    """Livreur assigné (ou administration pour une nouvelle tentative) :
    transition suivant la table de apps.livraison.services. « livrée »
    exige le code de livraison donné par le client."""
    permission_classes = [IsAuthenticated, EstLivreurOuAdministrateur]
    throttle_scope = "livraison_statut"

    def patch(self, request, pk):
        livraison = get_object_or_404(Livraison, pk=pk)
        serializer = LivraisonChangerStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        donnees = serializer.validated_data
        try:
            livraison = services.changer_statut(
                livraison, donnees["status"], request.user,
                commentaire=donnees["commentaire"], code=donnees["code"],
            )
        except (services.LivraisonRefusee, TransitionImpossible) as erreur:
            return _refus(erreur)
        return Response(_representation(request.user, livraison), status=status.HTTP_200_OK)


class LivraisonHistoriqueListView(generics.ListAPIView):
    """Historique : mêmes droits que le détail (404 pour un tiers)."""
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        livraison, _ = livraison_visible(self.request.user, self.kwargs["livraison_id"])
        return LivraisonHistorique.objects.filter(livraison=livraison).select_related("effectue_par")

    def get_serializer_class(self):
        if self.request.user.role in ROLES_ADMINISTRATION:
            return LivraisonHistoriqueAdministrationSerializer
        return LivraisonHistoriqueSerializer


# =====================================================================
# CLIENT : CONTESTATION « NON REÇU »
# =====================================================================

class ContesterLivraisonView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "livraison_contestation"

    def post(self, request, pk):
        livraison = get_object_or_404(Livraison, pk=pk, commande__client=request.user)
        serializer = ContesterLivraisonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            contestation = services.contester_livraison(livraison, request.user, serializer.validated_data["motif"])
        except services.LivraisonRefusee as erreur:
            return _refus(erreur)
        return Response(ContestationSerializer(contestation).data, status=status.HTTP_201_CREATED)


# =====================================================================
# VENDEUR : LECTURE SEULE
# =====================================================================

def livraisons_du_vendeur(utilisateur):
    return Livraison.objects.filter(commande__boutique__proprietaire=utilisateur).select_related("commande", "livreur")


class LivraisonVendeurListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, EstVendeurValide]
    serializer_class = LivraisonVendeurSerializer

    def get_queryset(self):
        return livraisons_du_vendeur(self.request.user)


class LivraisonVendeurDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated, EstVendeurValide]
    serializer_class = LivraisonVendeurSerializer

    def get_queryset(self):
        return livraisons_du_vendeur(self.request.user)


# =====================================================================
# ADMINISTRATION
# =====================================================================

class AssignerLivreurView(APIView):
    permission_classes = [IsAuthenticated, EstAdministrateur]

    def post(self, request, pk):
        livraison = get_object_or_404(Livraison, pk=pk)
        serializer = AssignerLivreurSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        livreur = Utilisateur.objects.filter(pk=serializer.validated_data["livreur_id"]).first()
        if livreur is None:
            return Response({"detail": "Livreur introuvable."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            livraison = services.assigner_livreur(
                livraison, livreur, request.user,
                date_livraison_estimee=serializer.validated_data.get("date_livraison_estimee"),
            )
        except services.LivraisonRefusee as erreur:
            return _refus(erreur)
        return Response(_representation(request.user, livraison), status=status.HTTP_200_OK)


class AbandonnerLivraisonView(APIView):
    """Échec définitif : la commande est annulée, le paiement devient un
    remboursement à traiter."""
    permission_classes = [IsAuthenticated, EstAdministrateur]

    def post(self, request, pk):
        livraison = get_object_or_404(Livraison, pk=pk)
        serializer = AbandonnerLivraisonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            livraison = services.abandonner_livraison(livraison, request.user, serializer.validated_data["commentaire"])
        except (services.LivraisonRefusee, TransitionImpossible) as erreur:
            return _refus(erreur)
        return Response(_representation(request.user, livraison), status=status.HTTP_200_OK)


class ResoudreContestationView(APIView):
    permission_classes = [IsAuthenticated, EstAdministrateur]

    def post(self, request, pk):
        livraison = get_object_or_404(Livraison, pk=pk)
        serializer = ResoudreContestationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            contestation = services.resoudre_contestation(
                livraison, request.user, serializer.validated_data["decision"],
                serializer.validated_data["commentaire"],
            )
        except services.LivraisonRefusee as erreur:
            return _refus(erreur)
        return Response(ContestationSerializer(contestation).data, status=status.HTTP_200_OK)


def _livreurs():
    return Utilisateur.objects.filter(role=Role.LIVREUR).annotate(
        livraisons_en_cours=Count(
            "livraisons_assignees",
            filter=~Q(livraisons_assignees__status__in=services.STATUTS_TERMINES),
        ),
    ).order_by("prenom", "nom")


class LivreurListView(generics.ListAPIView):
    """Livreurs actifs, pour l'assignation."""
    permission_classes = [IsAuthenticated, EstAdministrateur]
    serializer_class = LivreurSerializer

    def get_queryset(self):
        return _livreurs().filter(is_active=True)


class NommerLivreurView(APIView):
    permission_classes = [IsAuthenticated, EstAdministrateur]

    def post(self, request):
        serializer = NommerLivreurSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        utilisateur = get_object_or_404(Utilisateur, pk=serializer.validated_data["utilisateur_id"])
        try:
            services.nommer_livreur(utilisateur, request.user)
        except services.LivraisonRefusee as erreur:
            return _refus(erreur)
        return Response(LivreurSerializer(_livreurs().get(pk=utilisateur.pk)).data, status=status.HTTP_200_OK)


class RetirerLivreurView(APIView):
    """Retrait immédiat du rôle ; renvoie les livraisons à réassigner."""
    permission_classes = [IsAuthenticated, EstAdministrateur]

    def post(self, request, pk):
        utilisateur = get_object_or_404(Utilisateur, pk=pk)
        try:
            utilisateur, a_reassigner = services.retirer_livreur(utilisateur, request.user)
        except services.LivraisonRefusee as erreur:
            return _refus(erreur)
        return Response({
            "utilisateur": {"id": utilisateur.pk, "prenom": utilisateur.prenom, "nom": utilisateur.nom,
                            "role": utilisateur.role},
            "livraisons_a_reassigner": LivraisonAReassignerSerializer(a_reassigner, many=True).data,
        }, status=status.HTTP_200_OK)
