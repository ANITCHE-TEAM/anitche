"""Endpoints du module vendeurs.

Trois niveaux d'accès, volontairement séparés dans les URL :
  - public            : vitrine des boutiques (`/boutiques/`)
  - vendeur authentifié : sa propre boutique (`/ma-boutique/`)
  - administration     : instruction des demandes (`/administration/...`)
"""

from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Boutique, DemandeVendeur
from .permissions import (
    EstAdministrateur,
    EstProprietaireDeLaBoutique,
    EstVendeurValide,
)
from .serializers import (
    BoutiqueAdministrationSerializer,
    BoutiquePubliqueSerializer,
    BoutiqueSerializer,
    DecisionVendeurSerializer,
    DemandeVendeurSerializer,
    RefusVendeurSerializer,
)
from .services import (
    TransitionVendeurImpossible,
    refuser_demande_vendeur,
    valider_demande_vendeur,
)


# --------------------------------------------------------------------------
# Public
# --------------------------------------------------------------------------

class BoutiquePubliqueListView(generics.ListAPIView):
    """Liste des boutiques ouvertes tenues par un vendeur validé."""

    serializer_class = BoutiquePubliqueSerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        queryset = Boutique.objects.publiques().select_related('proprietaire')

        recherche = self.request.query_params.get('recherche')
        if recherche:
            queryset = queryset.filter(nom__icontains=recherche)

        ville = self.request.query_params.get('ville')
        if ville:
            queryset = queryset.filter(ville__iexact=ville)

        return queryset


class BoutiquePubliqueDetailView(generics.RetrieveAPIView):
    """Fiche publique d'une boutique, adressée par son slug."""

    serializer_class = BoutiquePubliqueSerializer
    permission_classes = [AllowAny]
    lookup_field = 'slug'

    def get_queryset(self):
        return Boutique.objects.publiques().select_related('proprietaire')


# --------------------------------------------------------------------------
# Vendeur authentifié
# --------------------------------------------------------------------------

class MaBoutiqueView(generics.RetrieveUpdateAPIView):
    """GET / PATCH / PUT : la boutique du vendeur connecté. POST : la créer.

    La création exige un compte vendeur validé ; la consultation et la mise à
    jour sont réservées au propriétaire.
    """

    serializer_class = BoutiqueSerializer
    permission_classes = [IsAuthenticated, EstVendeurValide, EstProprietaireDeLaBoutique]

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
            compte = self.service(demande, serializer.validated_data['commentaire'])
        except TransitionVendeurImpossible as erreur:
            return Response({"message": str(erreur)}, status=status.HTTP_400_BAD_REQUEST)

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


class ValiderDemandeVendeurView(DecisionVendeurView):
    service = staticmethod(valider_demande_vendeur)
    message_succes = "Demande validée : le compte est désormais vendeur."


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
