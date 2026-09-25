from django.db import IntegrityError
from django.db.models import Exists, OuterRef
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from rest_framework.throttling import ScopedRateThrottle

from .models import PasseportProduit, viole_unicite_lot
from .permissions import BoutiqueDuVendeurNonSuspendue, EstVendeurValideOuAdministrateur
from .serializers import (
    MESSAGE_LOT_DEJA_CERTIFIE,
    PasseportPublicSerializer,
    PasseportRevoqueSerializer,
    PasseportVendeurSerializer,
    CreerPasseportSerializer,
    origine_desactivation,
)
from apps.catalogue.models import Produit
from apps.core.reseau import adresse_ip_client
from apps.vendeurs.permissions import ROLES_ADMINISTRATION

PERMISSIONS_ESPACE_VENDEUR = [EstVendeurValideOuAdministrateur, BoutiqueDuVendeurNonSuspendue]


def passeports_accessibles(utilisateur):
    """Tous les passeports pour l'administration, sinon ceux de sa boutique."""
    passeports = PasseportProduit.objects.select_related("produit", "variante", "boutique")
    if utilisateur.role in ROLES_ADMINISTRATION:
        return passeports
    return passeports.filter(boutique__proprietaire=utilisateur)


class PasseportPublicVerificationView(APIView):
    """Consultation publique et vérification d'authenticité d'un produit via son code passeport."""

    permission_classes = [AllowAny]
    # Limite dédiée, par IP : les clients d'un même opérateur mobile
    # partagent souvent une IP publique (CGNAT), le taux 'anon' global
    # (partagé avec toute l'API) serait trop bas pour un scan en magasin.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "passeport_verification"

    def get(self, request, code_passeport):
        code = code_passeport.strip().upper()
        passeport = (
            PasseportProduit.objects
            .select_related("produit", "variante", "boutique")
            # Même règle que la fiche publique du catalogue (404 si absent).
            .annotate(produit_visible=Exists(
                Produit.objects.visibles_publiquement().filter(pk=OuterRef("produit_id"))
            ))
            .filter(code_passeport=code)
            .first()
        )

        if not passeport:
            return Response(
                {"detail": f"Passeport numérique introuvable pour le code '{code}'."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Un scan de passeport révoqué est aussi journalisé : un code
        # révoqué encore scanné peut signaler des QR recopiés.
        passeport.enregistrer_scan(
            ip=adresse_ip_client(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )

        serializer_class = PasseportPublicSerializer if passeport.est_actif else PasseportRevoqueSerializer
        return Response(serializer_class(passeport).data, status=status.HTTP_200_OK)


class PasseportVendeurListCreateView(generics.ListAPIView):
    """Espace vendeur : lister (paginé) et créer les passeports numériques des produits de sa boutique."""

    permission_classes = PERMISSIONS_ESPACE_VENDEUR
    serializer_class = PasseportVendeurSerializer

    def get_queryset(self):
        return passeports_accessibles(self.request.user)

    def post(self, request):
        serializer = CreerPasseportSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        try:
            passeport = PasseportProduit.objects.create(
                produit=data["_produit"],
                variante=data["_variante"],
                boutique=data["_boutique"],
                numero_lot=data["numero_lot"],
                origine_geographique=data["origine_geographique"],
                materiaux_utilises=data["materiaux_utilises"],
                date_fabrication=data.get("date_fabrication"),
                artisan_createur=data["artisan_createur"],
                statut_certification=data["statut_certification"],
            )
        except IntegrityError as erreur:
            # Deux créations simultanées du même lot : la contrainte tranche.
            if viole_unicite_lot(erreur):
                raise ValidationError({"numero_lot": MESSAGE_LOT_DEJA_CERTIFIE})
            raise

        return Response(PasseportVendeurSerializer(passeport).data, status=status.HTTP_201_CREATED)


class PasseportVendeurDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Détail, mise à jour ou révocation d'un passeport par le vendeur propriétaire."""

    permission_classes = PERMISSIONS_ESPACE_VENDEUR
    serializer_class = PasseportVendeurSerializer

    def get_queryset(self):
        return passeports_accessibles(self.request.user)

    def perform_update(self, serializer):
        try:
            serializer.save()
        except IntegrityError as erreur:
            if viole_unicite_lot(erreur):
                raise ValidationError({"numero_lot": MESSAGE_LOT_DEJA_CERTIFIE})
            raise

    def perform_destroy(self, instance):
        """Révocation, jamais de suppression : un QR déjà imprimé doit
        continuer d'afficher « certificat révoqué » et non « introuvable »,
        et l'historique des scans est conservé."""
        instance.desactiver(par=origine_desactivation(self.request.user))
