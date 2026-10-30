from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import generics, status
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.catalogue.models import Produit
from apps.core.authentification import JWTAuthentificationOptionnelle
from apps.core.exceptions import ErreurMetier
from apps.utilisateurs.permissions import EmailVerifie
from apps.vendeurs.permissions import EstVendeurValide
from config.schema import erreurs

from . import services
from .models import Avis, SignalementAvis
from .permissions import EstModerateurAvis
from .serializers import (
    ArticleANoterSerializer,
    AvisPublicSerializer,
    AvisRecuSerializer,
    AvisSerializer,
    ContenuReponseSerializer,
    CreerAvisSerializer,
    DecisionModerationSerializer,
    FiltreSignalementsSerializer,
    ModifierAvisSerializer,
    MotifModerationSerializer,
    ReponseVendeurSerializer,
    SignalementModerationSerializer,
    SignalementSerializer,
    SignalerAvisSerializer,
)


def erreur_refus(refus):
    """Refus du service des avis → exception au format d'erreur commun."""
    return ErreurMetier(refus.message, refus.code_http)


def avis_visibles_de_l_auteur(utilisateur):
    """Les avis du client, supprimés exclus (ils n'existent plus pour lui)."""
    return (
        Avis.objects.filter(client=utilisateur)
        .exclude(statut=Avis.Statut.SUPPRIME)
        .select_related("produit", "boutique", "reponse")
    )


@extend_schema(
    summary="Articles que je peux noter",
    description=(
        "Lignes de mes commandes livrées, dans le délai pour noter, dont le produit n'a pas encore "
        "reçu mon avis. Les produits de ma propre boutique n'y figurent pas."
    ),
    responses={200: ArticleANoterSerializer(many=True)},
)
class ArticlesANoterListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = ArticleANoterSerializer

    def get_queryset(self):
        return services.articles_a_noter(self.request.user)


@extend_schema(summary="Mes avis", responses={200: AvisSerializer(many=True)})
class MesAvisListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = AvisSerializer

    def get_queryset(self):
        return avis_visibles_de_l_auteur(self.request.user)


@extend_schema(
    summary="Publier un avis",
    description=(
        "Réservé à l'acheteur d'un article livré (email vérifié), dans les 60 jours suivant la livraison. "
        "Un seul avis par produit. Commande non livrée ou délai écoulé : 400 ; produit de ma propre "
        "boutique : 403 ; article d'un autre client : 404 ; produit déjà noté : 409."
    ),
    request=CreerAvisSerializer,
    responses={201: AvisSerializer, **erreurs(403, 404, 409)},
)
class AvisCreerView(APIView):
    permission_classes = [IsAuthenticated, EmailVerifie]
    throttle_scope = "avis_creation"

    def post(self, request):
        serializer = CreerAvisSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        donnees = serializer.validated_data
        try:
            avis = services.creer_avis(
                request.user, donnees["commande_item_id"], donnees["note"], donnees["commentaire"],
            )
        except services.AvisRefuse as refus:
            raise erreur_refus(refus)
        avis = avis_visibles_de_l_auteur(request.user).get(pk=avis.pk)
        return Response(AvisSerializer(avis).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(summary="Détail de mon avis", responses={200: AvisSerializer}),
    patch=extend_schema(
        summary="Modifier mon avis",
        description=(
            "Note et/ou commentaire, pendant 14 jours après la publication. L'avis est alors marqué "
            "`est_modifie`. Avis masqué par la modération : 403 ; délai écoulé : 400."
        ),
        request=ModifierAvisSerializer,
        responses={200: AvisSerializer, **erreurs(403)},
    ),
    delete=extend_schema(
        summary="Supprimer mon avis",
        description="Suppression douce : l'avis sort de la note du produit, et le produit ne peut plus être noté.",
        responses={204: None, **erreurs(403)},
    ),
)
class AvisDetailView(APIView):
    """Détail, modification et suppression de son propre avis. L'avis d'un
    autre client est introuvable (404), jamais interdit."""

    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        # Limite dédiée aux écritures ; la lecture garde le taux général.
        self.throttle_scope = "avis_ecriture" if self.request.method in ("PATCH", "DELETE") else None
        return super().get_throttles()

    def get(self, request, pk):
        avis = get_object_or_404(avis_visibles_de_l_auteur(request.user), pk=pk)
        return Response(AvisSerializer(avis).data)

    def patch(self, request, pk):
        serializer = ModifierAvisSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            services.modifier_avis(
                pk, request.user,
                note=serializer.validated_data.get("note"),
                commentaire=serializer.validated_data.get("commentaire"),
            )
        except services.AvisRefuse as refus:
            raise erreur_refus(refus)
        avis = get_object_or_404(avis_visibles_de_l_auteur(request.user), pk=pk)
        return Response(AvisSerializer(avis).data)

    def delete(self, request, pk):
        try:
            services.supprimer_avis(pk, request.user)
        except services.AvisRefuse as refus:
            raise erreur_refus(refus)
        return Response(status=status.HTTP_204_NO_CONTENT)


# =====================================================================
# LECTURE PUBLIQUE ET ESPACE VENDEUR
# =====================================================================

@extend_schema(
    summary="Avis publiés d'un produit",
    description=(
        "Public. Seuls les avis publiés figurent ici, les plus récents d'abord, avec la réponse de la "
        "boutique. Un avis masqué par la modération ou supprimé par son auteur n'y est plus. "
        "Produit inconnu ou non visible au public : 404."
    ),
    responses={200: AvisPublicSerializer(many=True), **erreurs(404)},
)
class ProduitAvisListView(generics.ListAPIView):
    authentication_classes = [JWTAuthentificationOptionnelle]
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "catalogue_public"
    serializer_class = AvisPublicSerializer

    def get_queryset(self):
        produit_id = self.kwargs["produit_id"]
        if not Produit.objects.visibles_publiquement().filter(pk=produit_id).exists():
            raise NotFound("Produit introuvable.")
        return Avis.objects.filter(produit_id=produit_id, statut=Avis.Statut.PUBLIE).select_related(
            "client", "reponse",
        )


@extend_schema(
    summary="Avis reçus par ma boutique",
    description=(
        "Avis publiés sur les produits de ma boutique, les plus récents d'abord, avec ma réponse. "
        "`sans_reponse=true` ne garde que ceux auxquels je n'ai pas encore répondu."
    ),
    parameters=[
        OpenApiParameter("sans_reponse", bool, description="`true` : avis sans réponse de la boutique."),
    ],
    responses={200: AvisRecuSerializer(many=True), **erreurs(403)},
)
class AvisRecusListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, EstVendeurValide]
    serializer_class = AvisRecuSerializer

    def get_queryset(self):
        queryset = Avis.objects.filter(
            boutique__proprietaire=self.request.user, statut=Avis.Statut.PUBLIE,
        ).select_related("client", "produit", "reponse")
        if self.request.query_params.get("sans_reponse") == "true":
            queryset = queryset.filter(reponse__isnull=True)
        return queryset


@extend_schema_view(
    post=extend_schema(
        summary="Répondre à un avis",
        description=(
            "Réservé au propriétaire de la boutique de l'avis. Une seule réponse par avis (409 ensuite : "
            "la modifier). Avis d'une autre boutique ou boutique suspendue : 403 ; avis introuvable, masqué "
            "ou supprimé : 404."
        ),
        request=ContenuReponseSerializer,
        responses={201: ReponseVendeurSerializer, **erreurs(403, 404, 409)},
    ),
    patch=extend_schema(
        summary="Modifier ma réponse à un avis",
        description="Sans limite de durée ; `date_modification` est mise à jour.",
        request=ContenuReponseSerializer,
        responses={200: ReponseVendeurSerializer, **erreurs(403, 404)},
    ),
)
class ReponseAvisView(APIView):
    permission_classes = [IsAuthenticated, EstVendeurValide]
    throttle_scope = "avis_ecriture"

    def post(self, request, pk):
        serializer = ContenuReponseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            reponse = services.repondre_a_un_avis(pk, request.user, serializer.validated_data["contenu"])
        except services.AvisRefuse as refus:
            raise erreur_refus(refus)
        return Response(ReponseVendeurSerializer(reponse).data, status=status.HTTP_201_CREATED)

    def patch(self, request, pk):
        serializer = ContenuReponseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            reponse = services.modifier_reponse(pk, request.user, serializer.validated_data["contenu"])
        except services.AvisRefuse as refus:
            raise erreur_refus(refus)
        return Response(ReponseVendeurSerializer(reponse).data)


# =====================================================================
# SIGNALEMENT
# =====================================================================

@extend_schema(
    summary="Signaler un avis",
    description=(
        "Tout compte connecté. `motif` parmi une liste fermée, `commentaire` facultatif (500 caractères). "
        "Un signalement par utilisateur et par avis (409 ensuite). Son propre avis : 403 ; avis introuvable, "
        "masqué ou supprimé : 404. Le signalement ne masque rien : la modération décide."
    ),
    request=SignalerAvisSerializer,
    responses={201: SignalementSerializer, **erreurs(403, 404, 409)},
)
class SignalerAvisView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "avis_signalement"

    def post(self, request, pk):
        serializer = SignalerAvisSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            signalement = services.signaler_avis(
                pk, request.user, serializer.validated_data["motif"], serializer.validated_data["commentaire"],
            )
        except services.AvisRefuse as refus:
            raise erreur_refus(refus)
        return Response(SignalementSerializer(signalement).data, status=status.HTTP_201_CREATED)


# =====================================================================
# MODÉRATION
# =====================================================================

@extend_schema(
    summary="Signalements d'avis (modération)",
    description=(
        "Modérateur, admin, super_admin. File des signalements `ouvert` (défaut) dont l'avis est encore "
        "publié, les plus anciens d'abord ; `statut=accepte` ou `rejete` consulte les signalements traités. "
        "Valeur invalide : 400."
    ),
    parameters=[FiltreSignalementsSerializer],
    responses={200: SignalementModerationSerializer(many=True), **erreurs(400, 403)},
)
class SignalementsModerationListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, EstModerateurAvis]
    serializer_class = SignalementModerationSerializer

    def get_queryset(self):
        filtres = FiltreSignalementsSerializer(data=self.request.query_params)
        filtres.is_valid(raise_exception=True)
        statut = filtres.validated_data["statut"]
        queryset = SignalementAvis.objects.filter(statut=statut).select_related(
            "avis__produit", "avis__boutique",
        )
        if statut == SignalementAvis.Statut.OUVERT:
            queryset = queryset.filter(avis__statut=Avis.Statut.PUBLIE)
        return queryset


class DecisionModerationView(APIView):
    """Socle des décisions : motif obligatoire, réponse = la décision tracée."""

    permission_classes = [IsAuthenticated, EstModerateurAvis]
    decision = None

    def post(self, request, pk):
        serializer = MotifModerationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            decision = type(self).decision(pk, request.user, serializer.validated_data["motif"])
        except services.AvisRefuse as refus:
            raise erreur_refus(refus)
        return Response(DecisionModerationSerializer(decision).data, status=status.HTTP_201_CREATED)


@extend_schema(
    summary="Masquer un avis (modération)",
    description=(
        "Modérateur, admin, super_admin. `motif` obligatoire. L'avis sort du public et des moyennes "
        "(recalculées) ; son auteur le voit `masque`. Ses signalements ouverts sont acceptés. "
        "Avis non publié : 409."
    ),
    request=MotifModerationSerializer,
    responses={201: DecisionModerationSerializer, **erreurs(400, 403, 404, 409)},
)
class MasquerAvisView(DecisionModerationView):
    decision = staticmethod(services.masquer_avis)


@extend_schema(
    summary="Rétablir un avis masqué (modération)",
    description=(
        "Modérateur, admin, super_admin. `motif` obligatoire. L'avis revient dans le public et les moyennes. "
        "Avis non masqué : 409."
    ),
    request=MotifModerationSerializer,
    responses={201: DecisionModerationSerializer, **erreurs(400, 403, 404, 409)},
)
class RetablirAvisView(DecisionModerationView):
    decision = staticmethod(services.retablir_avis)


@extend_schema(
    summary="Rejeter un signalement (modération)",
    description=(
        "Modérateur, admin, super_admin. `motif` obligatoire. L'avis reste publié. "
        "Signalement déjà traité : 409."
    ),
    request=MotifModerationSerializer,
    responses={201: DecisionModerationSerializer, **erreurs(400, 403, 404, 409)},
)
class RejeterSignalementView(DecisionModerationView):
    decision = staticmethod(services.rejeter_signalement)
