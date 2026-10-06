import logging

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.commandes.services import TransitionImpossible
from apps.core.authentification import JWTAuthentificationOptionnelle
from apps.core.exceptions import ErreurMetier
from drf_spectacular.utils import OpenApiParameter, PolymorphicProxySerializer, extend_schema

from config.schema import erreurs
from apps.utilisateurs.models import Role, Utilisateur
from apps.vendeurs.permissions import ROLES_ADMINISTRATION, EstAdministrateur, EstVendeurValide

from . import services
from .frais import grille_publique
from .models import ContestationLivraison, Livraison, LivraisonHistorique, TarifLivraison
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
    GrilleTarifsSerializer,
    RetraitLivreurSerializer,
    TarifLivraisonSerializer,
)

logger_securite = logging.getLogger("securite")


def livraison_selon_role(many=False):
    """Représentation d'une livraison selon le rôle du compte (schéma OpenAPI)."""
    return PolymorphicProxySerializer(
        component_name="LivraisonSelonRole",
        serializers=[LivraisonClientSerializer, LivraisonLivreurSerializer, LivraisonAdministrationSerializer],
        resource_type_field_name=None,
        many=many,
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
    """Refus d'un service de livraison → exception au format d'erreur commun."""
    if isinstance(erreur, TransitionImpossible):
        return ErreurMetier(str(erreur), status.HTTP_409_CONFLICT)
    return ErreurMetier(str(erreur), erreur.code_http)


@extend_schema(
    summary="Mes livraisons (selon le rôle)",
    description=(
        "Client : livraisons de ses commandes ; livreur : celles qui lui sont assignées ; administration : toutes. "
        "La représentation dépend du rôle (LivraisonClient, LivraisonLivreur ou LivraisonAdministration). "
        "Filtres `status` et `contestation` réservés à l'administration (ignorés sinon)."
    ),
    parameters=[
        OpenApiParameter("status", enum=[valeur for valeur, _ in Livraison.Status.choices], description="Administration."),
        OpenApiParameter("contestation", enum=["ouverte"], description="Administration : contestations ouvertes."),
    ],
    responses={200: livraison_selon_role(many=True)},
)
class LivraisonListView(generics.ListAPIView):
    """Liste filtrée par rôle. Administration : filtres `?status=` et
    `?contestation=ouverte`."""
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # génération du schéma OpenAPI
            return Livraison.objects.none()
        queryset, _ = livraisons_visibles(self.request.user)
        if self.request.user.role in ROLES_ADMINISTRATION:
            statut = self.request.query_params.get("status")
            if statut:
                queryset = queryset.filter(status=statut)
            if self.request.query_params.get("contestation") == ContestationLivraison.Statut.OUVERTE:
                queryset = queryset.filter(contestation__statut=ContestationLivraison.Statut.OUVERTE)
        return queryset

    def get_serializer_class(self):
        if getattr(self, "swagger_fake_view", False):  # génération du schéma OpenAPI
            return LivraisonAdministrationSerializer
        return livraisons_visibles(self.request.user)[1]


@extend_schema(
    summary="Détail d'une livraison (selon le rôle)",
    responses={200: livraison_selon_role(), **erreurs(404)},
)
class LivraisonDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        livraison, serializer = livraison_visible(request.user, pk)
        return Response(serializer(livraison).data)


@extend_schema(
    summary="Changer le statut d'une livraison",
    description=(
        "Livreur assigné, ou administration pour une nouvelle tentative. « livree » exige le code de livraison "
        "(400 si faux, essais restants dans `detail`) ; « echouee » exige un commentaire."
    ),
    request=LivraisonChangerStatusSerializer,
    responses={200: livraison_selon_role(), **erreurs(409)},
)
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
            raise _refus(erreur)
        return Response(_representation(request.user, livraison), status=status.HTTP_200_OK)


class LivraisonHistoriqueListView(generics.ListAPIView):
    """Historique : mêmes droits que le détail (404 pour un tiers)."""
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # génération du schéma OpenAPI
            return LivraisonHistorique.objects.none()
        livraison, _ = livraison_visible(self.request.user, self.kwargs["livraison_id"])
        return LivraisonHistorique.objects.filter(livraison=livraison).select_related("effectue_par")

    def get_serializer_class(self):
        if getattr(self, "swagger_fake_view", False):  # génération du schéma OpenAPI
            return LivraisonHistoriqueSerializer
        if self.request.user.role in ROLES_ADMINISTRATION:
            return LivraisonHistoriqueAdministrationSerializer
        return LivraisonHistoriqueSerializer


# =====================================================================
# CLIENT : CONTESTATION « NON REÇU »
# =====================================================================

@extend_schema(
    summary="Signaler un colis non reçu (client)",
    description="Possible sur une livraison « livrée », pendant le délai de contestation, une seule fois.",
    request=ContesterLivraisonSerializer,
    responses={201: ContestationSerializer},
)
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
            raise _refus(erreur)
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

@extend_schema(
    summary="Assigner un livreur (administration)",
    description="Livreur inexistant : 400 `errors.livreur_id`.",
    request=AssignerLivreurSerializer,
    responses={200: LivraisonAdministrationSerializer},
)
class AssignerLivreurView(APIView):
    permission_classes = [IsAuthenticated, EstAdministrateur]

    def post(self, request, pk):
        livraison = get_object_or_404(Livraison, pk=pk)
        serializer = AssignerLivreurSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        livreur = Utilisateur.objects.filter(pk=serializer.validated_data["livreur_id"]).first()
        if livreur is None:
            raise ValidationError({"livreur_id": ["Livreur introuvable."]})
        try:
            livraison = services.assigner_livreur(
                livraison, livreur, request.user,
                date_livraison_estimee=serializer.validated_data.get("date_livraison_estimee"),
            )
        except services.LivraisonRefusee as erreur:
            raise _refus(erreur)
        return Response(_representation(request.user, livraison), status=status.HTTP_200_OK)


@extend_schema(
    summary="Abandonner une livraison échouée (administration)",
    description="La commande est annulée et le paiement devient un remboursement à traiter.",
    request=AbandonnerLivraisonSerializer,
    responses={200: LivraisonAdministrationSerializer, **erreurs(409)},
)
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
            raise _refus(erreur)
        return Response(_representation(request.user, livraison), status=status.HTTP_200_OK)


@extend_schema(
    summary="Trancher une contestation (administration)",
    request=ResoudreContestationSerializer,
    responses={200: ContestationSerializer},
)
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
            raise _refus(erreur)
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


@extend_schema(
    summary="Nommer un client livreur (administration)",
    request=NommerLivreurSerializer,
    responses={200: LivreurSerializer, **erreurs(404)},
)
class NommerLivreurView(APIView):
    permission_classes = [IsAuthenticated, EstAdministrateur]

    def post(self, request):
        serializer = NommerLivreurSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        utilisateur = get_object_or_404(Utilisateur, pk=serializer.validated_data["utilisateur_id"])
        try:
            services.nommer_livreur(utilisateur, request.user)
        except services.LivraisonRefusee as erreur:
            raise _refus(erreur)
        return Response(LivreurSerializer(_livreurs().get(pk=utilisateur.pk)).data, status=status.HTTP_200_OK)


@extend_schema(
    summary="Retirer le rôle livreur (administration)",
    description="Effet immédiat ; renvoie les livraisons non terminées à réassigner.",
    request=None,
    responses={200: RetraitLivreurSerializer},
)
class RetirerLivreurView(APIView):
    """Retrait immédiat du rôle ; renvoie les livraisons à réassigner."""
    permission_classes = [IsAuthenticated, EstAdministrateur]

    def post(self, request, pk):
        utilisateur = get_object_or_404(Utilisateur, pk=pk)
        try:
            utilisateur, a_reassigner = services.retirer_livreur(utilisateur, request.user)
        except services.LivraisonRefusee as erreur:
            raise _refus(erreur)
        return Response({
            "utilisateur": {"id": utilisateur.pk, "prenom": utilisateur.prenom, "nom": utilisateur.nom,
                            "role": utilisateur.role},
            "livraisons_a_reassigner": LivraisonAReassignerSerializer(a_reassigner, many=True).data,
        }, status=status.HTTP_200_OK)


# =====================================================================
# TARIFS DE LIVRAISON
# =====================================================================

@extend_schema(
    summary="Grille publique des tarifs de livraison",
    description="Menu déroulant des communes du checkout, avec le tarif de chacune, et le tarif des autres villes.",
    responses={200: GrilleTarifsSerializer},
)
class TarifLivraisonPublicListView(APIView):
    """Menu déroulant du checkout : communes (celles du district d'Abidjan et
    les villes qui ont un tarif propre) avec le tarif appliqué à chacune, et
    le tarif des autres villes."""
    # Un jeton refusé (expiré, révoqué) est ignoré : la grille reste publique.
    authentication_classes = [JWTAuthentificationOptionnelle]
    permission_classes = [AllowAny]
    # Seau du catalogue seul : la limite 'anon' (50/h par IP) bloquerait le
    # checkout des visiteurs derrière un CGNAT.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "catalogue_public"

    def get(self, request):
        return Response(grille_publique())


class TarifLivraisonListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated, EstAdministrateur]
    serializer_class = TarifLivraisonSerializer
    queryset = TarifLivraison.objects.all()

    def perform_create(self, serializer):
        tarif = serializer.save(modifie_par=self.request.user)
        logger_securite.info("Tarif de livraison %s créé par admin_id=%s : %s", tarif.pk, self.request.user.id, tarif)


class TarifLivraisonDetailView(generics.RetrieveUpdateAPIView):
    """Modification d'un tarif : les commandes passées gardent le leur."""
    permission_classes = [IsAuthenticated, EstAdministrateur]
    serializer_class = TarifLivraisonSerializer
    queryset = TarifLivraison.objects.all()

    def perform_update(self, serializer):
        tarif = serializer.save(modifie_par=self.request.user)
        logger_securite.info("Tarif de livraison %s modifié par admin_id=%s : %s", tarif.pk, self.request.user.id, tarif)
