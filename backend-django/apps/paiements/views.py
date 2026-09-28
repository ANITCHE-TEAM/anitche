import logging

from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.exceptions import ErreurMetier
from drf_spectacular.utils import OpenApiParameter, PolymorphicProxySerializer, extend_schema, extend_schema_view

from config.schema import erreurs
from apps.vendeurs.permissions import ROLES_ADMINISTRATION, EstAdministrateur, EstVendeurValide

from . import reversements, services
from .fournisseurs.base import ErreurFournisseur, MontantHorsLimites
from .models import BaremeFrais, Paiement, Remboursement, Reversement
from .serializers import (
    CODE_BAREME_DEJA_APPLIQUE,
    BaremeFraisSerializer,
    InitierPaiementSerializer,
    PaiementAdminSerializer,
    PaiementSerializer,
    RemboursementAdminSerializer,
    ReversementAdminSerializer,
    ResumeReversementsSerializer,
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


def paiement_selon_role(many=False):
    """Paiement vu par le client ou par l'administration (schéma OpenAPI)."""
    return PolymorphicProxySerializer(
        component_name="PaiementSelonRole",
        serializers=[PaiementSerializer, PaiementAdminSerializer],
        resource_type_field_name=None,
        many=many,
    )


def serializer_paiement(utilisateur):
    return PaiementAdminSerializer if utilisateur.role in ROLES_ADMINISTRATION else PaiementSerializer


# =====================================================================
# CLIENT
# =====================================================================

@extend_schema(
    summary="Payer une commande ou un checkout entier",
    description=(
        "Indiquer `commande_id` ou `groupe_commande_id`. Rediriger ensuite vers `url_paiement` ; le statut final "
        "arrive par la notification serveur du fournisseur (interroger le détail du paiement)."
    ),
    request=InitierPaiementSerializer,
    responses={201: PaiementSerializer, **erreurs(502)},
)
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
            raise ErreurMetier(MESSAGE_FOURNISSEUR_INDISPONIBLE, status.HTTP_502_BAD_GATEWAY)
        return Response(PaiementSerializer(paiements_visibles(request.user).get(pk=paiement.pk)).data,
                        status=status.HTTP_201_CREATED)


@extend_schema(
    summary="Mes paiements (tous pour l'administration)",
    description="Représentation PaiementAdmin pour l'administration, Paiement sinon.",
    responses={200: paiement_selon_role(many=True)},
)
class PaiementListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # génération du schéma OpenAPI
            return Paiement.objects.none()
        return paiements_visibles(self.request.user)

    def get_serializer_class(self):
        if getattr(self, "swagger_fake_view", False):  # génération du schéma OpenAPI
            return PaiementSerializer
        return serializer_paiement(self.request.user)


@extend_schema(
    summary="Détail d'un paiement",
    description="Représentation PaiementAdmin pour l'administration, Paiement sinon.",
    responses={200: paiement_selon_role()},
)
class PaiementDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # génération du schéma OpenAPI
            return Paiement.objects.none()
        return paiements_visibles(self.request.user)

    def get_serializer_class(self):
        if getattr(self, "swagger_fake_view", False):  # génération du schéma OpenAPI
            return PaiementSerializer
        return serializer_paiement(self.request.user)


@extend_schema(
    summary="Abandonner un paiement en attente",
    description="Pour changer de moyen de paiement. 409 si le paiement n'est plus en attente.",
    request=None,
    responses={200: PaiementSerializer, **erreurs(409)},
)
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
            raise ErreurMetier(str(erreur), status.HTTP_409_CONFLICT)
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


@extend_schema(exclude=True)
class WebhookPaiementView(_VueNotification):
    def post(self, request, fournisseur):
        return self.repondre(services.traiter_notification_paiement(fournisseur, request))


@extend_schema(exclude=True)
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


@extend_schema(parameters=[OpenApiParameter("statut", enum=[valeur for valeur, _ in Reversement.Statut.choices])])
class ReversementVendeurListView(generics.ListAPIView):
    """Ce que le vendeur a vendu, les frais déduits, ce qui lui sera reversé et quand."""

    permission_classes = [IsAuthenticated, EstVendeurValide]
    serializer_class = ReversementVendeurSerializer

    def get_queryset(self):
        qs = reversements_du_vendeur(self.request.user)
        statut = self.request.query_params.get("statut")
        return qs.filter(statut=statut) if statut else qs


@extend_schema(
    summary="Résumé de mes reversements (vendeur)",
    responses={200: ResumeReversementsSerializer, **erreurs(404)},
)
class ResumeReversementsVendeurView(APIView):
    permission_classes = [IsAuthenticated, EstVendeurValide]

    def get(self, request):
        boutique = getattr(request.user, "boutique", None)
        if boutique is None:
            raise NotFound("Aucune boutique.")
        return Response(reversements.resume_vendeur(boutique))


# =====================================================================
# ADMINISTRATION
# =====================================================================

@extend_schema(parameters=[OpenApiParameter("statut", enum=[valeur for valeur, _ in Remboursement.Statut.choices])])
class RemboursementAdminListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, EstAdministrateur]
    serializer_class = RemboursementAdminSerializer

    def get_queryset(self):
        qs = Remboursement.objects.select_related("commande", "paiement__client", "retour")
        statut = self.request.query_params.get("statut")
        return qs.filter(statut=statut) if statut else qs


@extend_schema(
    summary="Traiter un remboursement (administration)",
    request=TraiterRemboursementSerializer,
    responses={200: RemboursementAdminSerializer, **erreurs(409)},
)
class TraiterRemboursementView(APIView):
    permission_classes = [IsAuthenticated, EstAdministrateur]

    def post(self, request, pk):
        remboursement = get_object_or_404(Remboursement, pk=pk)
        serializer = TraiterRemboursementSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            remboursement = services.traiter_remboursement(remboursement, request.user, **serializer.validated_data)
        except services.TransitionPaiementImpossible as erreur:
            raise ErreurMetier(str(erreur), status.HTTP_409_CONFLICT)
        remboursement = Remboursement.objects.select_related("commande", "paiement__client", "retour").get(pk=pk)
        return Response(RemboursementAdminSerializer(remboursement).data)


def reversements_admin():
    return Reversement.objects.select_related("commande", "boutique").prefetch_related("commande__article")


@extend_schema(parameters=[OpenApiParameter("statut", enum=[valeur for valeur, _ in Reversement.Statut.choices])])
class ReversementAdminListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, EstAdministrateur]
    serializer_class = ReversementAdminSerializer

    def get_queryset(self):
        qs = reversements_admin()
        statut = self.request.query_params.get("statut")
        return qs.filter(statut=statut) if statut else qs


@extend_schema(
    summary="Enregistrer un versement manuel (administration)",
    request=VerserReversementSerializer,
    responses={200: ReversementAdminSerializer, **erreurs(409)},
)
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
            raise ErreurMetier(str(erreur), status.HTTP_409_CONFLICT)
        return Response(ReversementAdminSerializer(reversements_admin().get(pk=pk)).data)


@extend_schema(
    summary="Verser par transfert du fournisseur (administration)",
    request=TransfererReversementSerializer,
    responses={200: ReversementAdminSerializer, **erreurs(409)},
)
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
            raise ErreurMetier(str(erreur), status.HTTP_409_CONFLICT)
        return Response(ReversementAdminSerializer(reversements_admin().get(pk=pk)).data)


@extend_schema_view(
    post=extend_schema(description=(
        "date_debut maintenant ou plus tard (une date passée de moins d'une minute vaut maintenant, ramenée à "
        "l'heure du serveur) ; plus ancienne : 400, errors.code = [\"date_debut_passee\"]. Absente : le barème "
        "commence à l'enregistrement, et ne pourra ensuite qu'être clôturé."
    )),
)
class BaremeFraisListCreateView(generics.ListCreateAPIView):
    """Barèmes de frais vendeur (plateforme ou offre de lancement d'une boutique)."""

    permission_classes = [IsAuthenticated, EstAdministrateur]
    serializer_class = BaremeFraisSerializer
    queryset = BaremeFrais.objects.select_related("boutique")

    def perform_create(self, serializer):
        bareme = serializer.save(cree_par=self.request.user)
        logger_securite.info("Barème de frais %s créé par admin_id=%s : %s", bareme.pk, self.request.user.id, bareme)


MODIFIER_UN_BAREME = (
    "Barème pas encore commencé (date_debut future) : tout se modifie, date_debut maintenant ou plus tard "
    "(sinon 400, errors.code = [\"date_debut_passee\"]). Barème commencé : seule date_fin, pour le clôturer "
    "maintenant ou plus tard ; toute autre modification, une date_fin passée, une réouverture ou un barème "
    "déjà clôturé : 400, errors.code = [\"bareme_deja_applique\"]. Une date passée de moins d'une minute vaut "
    "maintenant (ramenée à l'heure du serveur)."
)


@extend_schema_view(
    put=extend_schema(description=MODIFIER_UN_BAREME),
    patch=extend_schema(description=MODIFIER_UN_BAREME),
    delete=extend_schema(
        summary="Supprimer un barème pas encore commencé",
        description="Barème commencé : 409, errors.code = [\"bareme_deja_applique\"] (le clôturer avec date_fin).",
        responses={204: None, **erreurs(409)},
    ),
)
class BaremeFraisDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Un barème commencé a pu s'appliquer à des ventes : il ne se modifie
    plus, sauf pour le clôturer (date_fin), et ne se supprime pas."""

    permission_classes = [IsAuthenticated, EstAdministrateur]
    serializer_class = BaremeFraisSerializer
    queryset = BaremeFrais.objects.select_related("boutique")

    def perform_update(self, serializer):
        bareme = serializer.save()
        logger_securite.info("Barème de frais %s modifié par admin_id=%s : %s", bareme.pk, self.request.user.id, bareme)

    def perform_destroy(self, instance):
        if instance.a_commence():
            raise ErreurMetier(
                {"detail": "Barème déjà appliqué : il ne se supprime pas. Clôturez-le (date_fin).",
                 "code": CODE_BAREME_DEJA_APPLIQUE},
                status.HTTP_409_CONFLICT,
            )
        logger_securite.info("Barème de frais %s supprimé par admin_id=%s : %s",
                             instance.pk, self.request.user.id, instance)
        instance.delete()

