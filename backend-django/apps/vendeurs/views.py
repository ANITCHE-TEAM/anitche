"""Endpoints du module vendeurs.

Trois niveaux d'accès, volontairement séparés dans les URL :
  - public            : vitrine des boutiques (`/boutiques/`)
  - vendeur authentifié : sa propre boutique (`/ma-boutique/`)
  - administration     : instruction des demandes (`/administration/...`)
"""

import logging

from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.authentification import JWTAuthentificationOptionnelle
from apps.core.exceptions import ErreurMetier
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view

from config.schema import erreurs

from .models import Boutique, DemandeVendeur
from .permissions import (
    BoutiqueNonSuspendue,
    EstAdministrateur,
    EstProprietaireDeLaBoutique,
    EstVendeurValide,
)
from .serializers import (
    BoutiqueAdministrationSerializer,
    BoutiquePubliqueSerializer,
    BoutiqueSerializer,
    DecisionVendeurReponseSerializer,
    DecisionVendeurSerializer,
    DemandeVendeurSerializer,
    RefusVendeurSerializer,
)
from .services import (
    AutoApprobationInterdite,
    TransitionVendeurImpossible,
    refuser_demande_vendeur,
    valider_demande_vendeur,
)

logger_securite = logging.getLogger('securite')


# --------------------------------------------------------------------------
# Public
# --------------------------------------------------------------------------

@extend_schema(
    summary="Boutiques publiques",
    parameters=[
        OpenApiParameter("recherche", str, description="Partie du nom (100 caractères au plus)."),
        OpenApiParameter("ville", str, description="Ville exacte (insensible à la casse)."),
    ],
)
class BoutiquePubliqueListView(generics.ListAPIView):
    """Liste des boutiques ouvertes tenues par un vendeur validé."""

    serializer_class = BoutiquePubliqueSerializer
    # Un jeton refusé (expiré, révoqué) est ignoré : la vitrine reste publique.
    authentication_classes = [JWTAuthentificationOptionnelle]
    permission_classes = [AllowAny]
    # Même seau que le catalogue (même navigation), sans la limite 'anon'.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'catalogue_public'

    def get_queryset(self):
        queryset = Boutique.objects.publiques().select_related('proprietaire')

        recherche = self.request.query_params.get('recherche', '')[:100]
        if recherche:
            queryset = queryset.filter(nom__icontains=recherche)

        ville = self.request.query_params.get('ville', '')[:100]
        if ville:
            queryset = queryset.filter(ville__iexact=ville)

        return queryset


class BoutiquePubliqueDetailView(generics.RetrieveAPIView):
    """Fiche publique d'une boutique, adressée par son slug."""

    serializer_class = BoutiquePubliqueSerializer
    authentication_classes = [JWTAuthentificationOptionnelle]
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'catalogue_public'
    lookup_field = 'slug'

    def get_queryset(self):
        return Boutique.objects.publiques().select_related('proprietaire')


# --------------------------------------------------------------------------
# Vendeur authentifié
# --------------------------------------------------------------------------

@extend_schema_view(
    get=extend_schema(summary="Ma boutique", responses={200: BoutiqueSerializer, **erreurs(404)}),
    put=extend_schema(summary="Modifier ma boutique", responses={200: BoutiqueSerializer, **erreurs(404)}),
    patch=extend_schema(summary="Modifier ma boutique", responses={200: BoutiqueSerializer, **erreurs(404)}),
    post=extend_schema(
        summary="Créer ma boutique",
        description="Une seule boutique par compte (400 sinon).",
        responses={201: BoutiqueSerializer},
    ),
)
class MaBoutiqueView(generics.RetrieveUpdateAPIView):
    """GET / PATCH / PUT : la boutique du vendeur connecté. POST : la créer.

    Toutes les méthodes, lecture comprise, exigent un compte vendeur validé
    (403 sinon) ; consultation et mise à jour sont réservées au propriétaire,
    et une boutique suspendue n'est pas modifiable (403).
    """

    serializer_class = BoutiqueSerializer
    permission_classes = [
        IsAuthenticated, EstVendeurValide, EstProprietaireDeLaBoutique, BoutiqueNonSuspendue,
    ]

    def get_throttles(self):
        """
        Le scope 'boutique_creation' (taux bas, pensé pour limiter les
        tentatives de création) ne doit s'appliquer qu'au POST : posé via
        throttle_classes/throttle_scope de classe, il s'appliquerait aussi
        à GET/PATCH et bloquerait le dashboard vendeur (consultation/mise à
        jour répétées en usage normal). Les autres
        méthodes retombent sur les throttles par défaut (user/anon, voir
        REST_FRAMEWORK.DEFAULT_THROTTLE_CLASSES).
        """
        if self.request.method == 'POST':
            self.throttle_scope = 'boutique_creation'
            return [ScopedRateThrottle()]
        return super().get_throttles()

    def get_object(self):
        boutique = get_object_or_404(Boutique, proprietaire=self.request.user)
        self.check_object_permissions(self.request, boutique)
        return boutique

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


# --------------------------------------------------------------------------
# Administration
# --------------------------------------------------------------------------

class DemandesVendeurListView(generics.ListAPIView):
    """File des demandes vendeur en attente de décision."""

    serializer_class = DemandeVendeurSerializer
    permission_classes = [IsAuthenticated, EstAdministrateur]
    queryset = DemandeVendeur.objects.select_related('dossier_kyc').order_by('date_creation')


class DecisionVendeurView(APIView):
    """Base commune aux deux décisions admin : validation et refus."""

    permission_classes = [IsAuthenticated, EstAdministrateur]
    serializer_class = DecisionVendeurSerializer
    service = None
    message_succes = ''

    def post(self, request, pk):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)

        demande = get_object_or_404(DemandeVendeur, pk=pk)

        try:
            compte = self.service(
                demande,
                serializer.validated_data['commentaire'],
                decideur=request.user,
            )
        except AutoApprobationInterdite as erreur:
            raise PermissionDenied(str(erreur))
        except TransitionVendeurImpossible as erreur:
            raise ErreurMetier(str(erreur))

        return Response(
            {
                "message": self.message_succes,
                "utilisateur": {
                    "id": compte.id,
                    "email": compte.email,
                    "role": compte.role,
                    "statut_kyc": compte.statut_kyc,
                },
            },
            status=status.HTTP_200_OK,
        )


@extend_schema(
    summary="Valider une demande vendeur",
    description="Le compte devient vendeur (KYC validé). Auto-approbation : 403 ; demande hors file d'attente : 404.",
    request=DecisionVendeurSerializer,
    responses={200: DecisionVendeurReponseSerializer},
)
class ValiderDemandeVendeurView(DecisionVendeurView):
    service = staticmethod(valider_demande_vendeur)
    message_succes = "Demande validée : le compte est désormais vendeur."


@extend_schema(
    summary="Refuser une demande vendeur",
    description="Commentaire obligatoire (motif transmis au demandeur). Auto-décision : 403 ; demande hors file d'attente : 404.",
    request=RefusVendeurSerializer,
    responses={200: DecisionVendeurReponseSerializer},
)
class RefuserDemandeVendeurView(DecisionVendeurView):
    serializer_class = RefusVendeurSerializer
    service = staticmethod(refuser_demande_vendeur)
    message_succes = "Demande refusée."


class BoutiquesAdministrationListView(generics.ListAPIView):
    """Toutes les boutiques, y compris fermées ou rattachées à un vendeur suspendu."""

    serializer_class = BoutiqueAdministrationSerializer
    permission_classes = [IsAuthenticated, EstAdministrateur]
    queryset = Boutique.objects.select_related('proprietaire')


class BoutiqueAdministrationDetailView(generics.RetrieveUpdateAPIView):
    """Consultation et suspension/réactivation d'une boutique par le back-office."""

    serializer_class = BoutiqueAdministrationSerializer
    permission_classes = [IsAuthenticated, EstAdministrateur]
    queryset = Boutique.objects.select_related('proprietaire')

    def perform_update(self, serializer):
        etait_suspendue = serializer.instance.est_suspendue
        boutique = serializer.save()
        if etait_suspendue != boutique.est_suspendue:
            action = "suspendue" if boutique.est_suspendue else "réactivée"
            logger_securite.info(
                "Boutique %s (%s) %s par admin_id=%s",
                boutique.id, boutique.nom, action, self.request.user.id,
            )