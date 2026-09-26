import logging

from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.vendeurs.permissions import ROLES_ADMINISTRATION, EstAdministrateur, EstVendeurValide

from . import reversements, services
from .fournisseurs.base import ErreurFournisseur, MontantHorsLimites
from .models import BaremeFrais, Paiement, Remboursement, Reversement
from .serializers import (
    BaremeFraisSerializer,
    InitierPaiementSerializer,
    PaiementAdminSerializer,
    PaiementSerializer,
    RemboursementAdminSerializer,
    ReversementAdminSerializer,
    ReversementVendeurSerializer,
    TransfererReversementSerializer,
    TraiterRemboursementSerializer,
    VerserReversementSerializer,
)

logger_securite = logging.getLogger("securite")

MESSAGE_FOURNISSEUR_INDISPONIBLE = "Le service de paiement est momentanément indisponible. Réessayez dans un instant."


def paiements_visibles(utilisateur):
    """Client : ses paiements. Administration (rôle, jamais is_staff) : tous."""
    qs = Paiement.objects.select_related("client").prefetch_related("commandes", "remboursements")
    if utilisateur.role in ROLES_ADMINISTRATION:
        return qs
    return qs.filter(client=utilisateur)


def serializer_paiement(utilisateur):
    return PaiementAdminSerializer if utilisateur.role in ROLES_ADMINISTRATION else PaiementSerializer


# =====================================================================
# CLIENT
# =====================================================================

class InitierPaiementView(APIView):
    """Le client paie sa commande (ou son checkout entier) en ligne."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "paiements"

    def post(self, request):
        serializer = InitierPaiementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        donnees = serializer.validated_data
        try:
            paiement = services.initier_paiement(
                request.user, donnees["methode"],
                commande_id=donnees.get("commande_id"), groupe_commande_id=donnees.get("groupe_commande_id"),
            )
        except services.PaiementRefuse as refus:
            raise ValidationError(refus.args[0])
        except MontantHorsLimites as erreur:
            raise ValidationError(str(erreur))
        except ErreurFournisseur:
            return Response({"detail": MESSAGE_FOURNISSEUR_INDISPONIBLE}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(PaiementSerializer(paiements_visibles(request.user).get(pk=paiement.pk)).data,
                        status=status.HTTP_201_CREATED)


class PaiementListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return paiements_visibles(self.request.user)

    def get_serializer_class(self):
        return serializer_paiement(self.request.user)


class PaiementDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return paiements_visibles(self.request.user)

    def get_serializer_class(self):
        return serializer_paiement(self.request.user)


class AnnulerPaiementView(APIView):
    """Le client abandonne son paiement en attente pour en relancer un autre."""

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "paiements"

    def post(self, request, pk):
        paiement = get_object_or_404(Paiement, pk=pk, client=request.user)
        try:
            services.annuler_paiement_client(paiement)
        except services.TransitionPaiementImpossible as erreur:
            return Response({"detail": str(erreur)}, status=status.HTTP_409_CONFLICT)
        return Response(PaiementSerializer(paiements_visibles(request.user).get(pk=pk)).data)


# =====================================================================
# NOTIFICATIONS DES FOURNISSEURS
# =====================================================================

class _VueNotification(APIView):
    """Pas de JWT (les fournisseurs n'ont pas de compte ANITCHE) : chaque
    notification est authentifiée par l'adaptateur (signature ou jeton),
    puis revérifiée auprès du fournisseur. Limite dédiée par IP, haute,
    au lieu de la limite anonyme (50/heure) qui bloquait des paiements."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "webhook_paiement"

    def repondre(self, resultat):
        cle = "message" if resultat.code_http == 200 else "erreur"
        return Response({cle: resultat.message}, status=resultat.code_http)


class WebhookPaiementView(_VueNotification):
    def post(self, request, fournisseur):
        return self.repondre(services.traiter_notification_paiement(fournisseur, request))


class WebhookTransfertView(_VueNotification):
    def post(self, request, fournisseur):
        return self.repondre(reversements.traiter_notification_transfert(fournisseur, request))


# =====================================================================
# VENDEUR
# =====================================================================

def reversements_du_vendeur(utilisateur):
    return Reversement.objects.filter(boutique__proprietaire=utilisateur).select_related(
        "commande",
    ).prefetch_related("commande__article")


class ReversementVendeurListView(generics.ListAPIView):
    """Ce que le vendeur a vendu, les frais déduits, ce qui lui sera reversé et quand."""

    permission_classes = [IsAuthenticated, EstVendeurValide]
    serializer_class = ReversementVendeurSerializer

    def get_queryset(self):
        qs = reversements_du_vendeur(self.request.user)
        statut = self.request.query_params.get("statut")
        return qs.filter(statut=statut) if statut else qs


class ResumeReversementsVendeurView(APIView):
    permission_classes = [IsAuthenticated, EstVendeurValide]

    def get(self, request):
        boutique = getattr(request.user, "boutique", None)
        if boutique is None:
            return Response({"detail": "Aucune boutique."}, status=status.HTTP_404_NOT_FOUND)
        return Response(reversements.resume_vendeur(boutique))


# =====================================================================
# ADMINISTRATION
# =====================================================================

class RemboursementAdminListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, EstAdministrateur]
    serializer_class = RemboursementAdminSerializer

    def get_queryset(self):
        qs = Remboursement.objects.select_related("commande", "paiement__client", "retour")
        statut = self.request.query_params.get("statut")
        return qs.filter(statut=statut) if statut else qs


class TraiterRemboursementView(APIView):
    permission_classes = [IsAuthenticated, EstAdministrateur]

    def post(self, request, pk):
        remboursement = get_object_or_404(Remboursement, pk=pk)
        serializer = TraiterRemboursementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            remboursement = services.traiter_remboursement(remboursement, request.user, **serializer.validated_data)
        except services.TransitionPaiementImpossible as erreur:
            return Response({"detail": str(erreur)}, status=status.HTTP_409_CONFLICT)
        remboursement = Remboursement.objects.select_related("commande", "paiement__client", "retour").get(pk=pk)
        return Response(RemboursementAdminSerializer(remboursement).data)


def reversements_admin():
    return Reversement.objects.select_related("commande", "boutique").prefetch_related("commande__article")


class ReversementAdminListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, EstAdministrateur]
    serializer_class = ReversementAdminSerializer

    def get_queryset(self):
        qs = reversements_admin()
        statut = self.request.query_params.get("statut")
        return qs.filter(statut=statut) if statut else qs


class VerserReversementView(APIView):
    """Versement manuel : l'admin a envoyé l'argent et saisit la référence."""

    permission_classes = [IsAuthenticated, EstAdministrateur]

    def post(self, request, pk):
        reversement = get_object_or_404(Reversement, pk=pk)
        serializer = VerserReversementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            reversements.verser_manuellement(reversement, request.user, serializer.validated_data["reference_externe"])
        except reversements.ErreurVersement as erreur:
            return Response({"detail": str(erreur)}, status=status.HTTP_409_CONFLICT)
        return Response(ReversementAdminSerializer(reversements_admin().get(pk=pk)).data)


class TransfererReversementView(APIView):
    """Versement par l'API de transfert du fournisseur."""

    permission_classes = [IsAuthenticated, EstAdministrateur]

    def post(self, request, pk):
        reversement = get_object_or_404(Reversement, pk=pk)
        serializer = TransfererReversementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            reversements.transferer_reversement(reversement, request.user, serializer.validated_data.get("operateur"))
        except reversements.ErreurVersement as erreur:
            return Response({"detail": str(erreur)}, status=status.HTTP_409_CONFLICT)
        return Response(ReversementAdminSerializer(reversements_admin().get(pk=pk)).data)


class BaremeFraisListCreateView(generics.ListCreateAPIView):
    """Barèmes de frais vendeur (plateforme ou offre de lancement d'une boutique)."""

    permission_classes = [IsAuthenticated, EstAdministrateur]
    serializer_class = BaremeFraisSerializer
    queryset = BaremeFrais.objects.select_related("boutique")

    def perform_create(self, serializer):
        bareme = serializer.save(cree_par=self.request.user)
        logger_securite.info("Barème de frais %s créé par admin_id=%s : %s", bareme.pk, self.request.user.id, bareme)


class BaremeFraisDetailView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsAuthenticated, EstAdministrateur]
    serializer_class = BaremeFraisSerializer
    queryset = BaremeFrais.objects.select_related("boutique")

    def perform_update(self, serializer):
        bareme = serializer.save()
        logger_securite.info("Barème de frais %s modifié par admin_id=%s : %s", bareme.pk, self.request.user.id, bareme)

