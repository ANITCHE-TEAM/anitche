from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.exceptions import ErreurMetier
from apps.utilisateurs.permissions import EmailVerifie
from config.schema import erreurs

from . import services
from .models import Avis
from .serializers import (
    ArticleANoterSerializer,
    AvisSerializer,
    CreerAvisSerializer,
    ModifierAvisSerializer,
)


def erreur_refus(refus):
    """Refus du service des avis → exception au format d'erreur commun."""
    return ErreurMetier(refus.message, refus.code_http)


def avis_visibles_de_l_auteur(utilisateur):
    """Les avis du client, supprimés exclus (ils n'existent plus pour lui)."""
    return (
        Avis.objects.filter(client=utilisateur)
        .exclude(statut=Avis.Statut.SUPPRIME)
        .select_related("produit", "boutique")
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
