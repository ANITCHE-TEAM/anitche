import logging
import uuid

from django.db import transaction
from django.db.models import Case, Value, When
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.exceptions import ErreurMetier
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view

from config.schema import FICHIER_IMAGE, erreurs
from apps.vendeurs.permissions import ROLES_ADMINISTRATION

from . import services
from .models import DemandeRetour, PhotoRetour, RetourHistorique
from .serializers import (
    CreerDemandeRetourSerializer,
    DemandeRetourSerializer,
    PhotoRetourSerializer,
    RetourHistoriqueAdministrationSerializer,
    RetourHistoriqueSerializer,
    TraiterDemandeRetourSerializer,
)

logger_securite = logging.getLogger("securite")


def identifiant_valide(valeur):
    """UUID ou None (un identifiant mal formé est refusé ensuite par le serializer)."""
    try:
        return uuid.UUID(str(valeur))
    except (TypeError, ValueError):
        return None


def erreur_refus(refus):
    """Refus du service des retours → exception au format d'erreur commun."""
    return ErreurMetier(refus.message, refus.code_http)


@extend_schema_view(
    get=extend_schema(summary="Mes demandes de retour", responses={200: DemandeRetourSerializer(many=True)}),
    post=extend_schema(
        summary="Demander un retour",
        description=(
            "Commande livrée, dans les 7 jours suivant la livraison ; remboursement uniquement. "
            "Hors délai, commande non livrée ou déjà retournée : 400 avec le motif."
        ),
        request=CreerDemandeRetourSerializer,
        responses={201: DemandeRetourSerializer, **erreurs(403, 404)},
    ),
)
class DemandeRetourListCreateView(APIView):
    """Le client liste ses demandes de retour ou en crée une (commande livrée,
    dans le délai de retour)."""

    permission_classes = [IsAuthenticated]
    # APIView brut : la pagination par défaut ne s'applique pas d'elle-même.
    pagination_class = PageNumberPagination

    def get_throttles(self):
        # Limite dédiée à la création ; la liste garde le taux général.
        self.throttle_scope = "retour_creation" if self.request.method == "POST" else None
        return super().get_throttles()

    def get(self, request):
        retours = (
            DemandeRetour.objects.filter(client=request.user)
            .select_related("commande", "boutique", "client")
            .prefetch_related("articles__commande_item", "photos")
        )
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(retours, request, view=self)
        serializer = DemandeRetourSerializer(page, many=True, context={"request": request})
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        with transaction.atomic():
            commande_id = identifiant_valide(request.data.get("commande_id"))
            if commande_id:
                services.verrouiller_commande_du_client(commande_id, request.user)
            serializer = CreerDemandeRetourSerializer(data=request.data, context={"request": request})
            serializer.is_valid(raise_exception=True)
            demande = services.creer_demande(request.user, serializer.validated_data)

        demande = services.demandes_visibles(request.user).get(pk=demande.pk)
        return Response(DemandeRetourSerializer(demande, context={"request": request}).data, status=status.HTTP_201_CREATED)


class DemandeRetourDetailView(generics.RetrieveAPIView):
    """Détail : client de la demande, vendeur de la boutique, administration."""

    permission_classes = [IsAuthenticated]
    serializer_class = DemandeRetourSerializer

    def get_queryset(self):
        return services.demandes_visibles(self.request.user)


class EspaceVendeurRetoursListView(generics.ListAPIView):
    """Demandes de retour de la boutique du vendeur connecté (toutes pour
    l'administration)."""

    permission_classes = [IsAuthenticated]
    serializer_class = DemandeRetourSerializer

    def get_queryset(self):
        qs = services.demandes_visibles(self.request.user)
        if self.request.user.role in ROLES_ADMINISTRATION:
            return qs
        return qs.filter(boutique__proprietaire=self.request.user)


class EstModerationOuAdministration(BasePermission):
    def has_permission(self, request, view):
        return getattr(request.user, "role", None) in services.ROLES_MODERATION


@extend_schema(
    summary="File des retours en recours (modération et administration)",
    description=(
        "Demandes passées par la modération (escalade ou contestation), toutes boutiques : en_moderation "
        "puis en_arbitrage d'abord, les plus anciennes en tête. Filtre `statut`. Sans e-mail du client pour "
        "un modérateur. Autres rôles : 403."
    ),
    parameters=[OpenApiParameter("statut", enum=[valeur for valeur, _ in DemandeRetour.Statut.choices])],
)
class ModerationRetoursListView(generics.ListAPIView):
    permission_classes = [IsAuthenticated, EstModerationOuAdministration]
    serializer_class = DemandeRetourSerializer

    def get_queryset(self):
        qs = services.demandes_visibles(self.request.user).filter(date_moderation__isnull=False)
        statut = self.request.query_params.get("statut")
        if statut:
            if statut not in DemandeRetour.Statut.values:
                raise ValidationError({"statut": "Statut inconnu."})
            qs = qs.filter(statut=statut)
        priorite = Case(
            When(statut=DemandeRetour.Statut.EN_MODERATION, then=Value(0)),
            When(statut=DemandeRetour.Statut.EN_ARBITRAGE, then=Value(1)),
            default=Value(2),
        )
        return qs.order_by(priorite, "date_moderation", "pk")


@extend_schema(
    summary="Historique d'une demande de retour",
    description=(
        "Étapes de la demande, de la plus ancienne à la plus récente : statuts, action, origine (`source`) et "
        "motif. Le compte auteur (`auteur`) n'est renvoyé qu'à l'administration. 404 pour un tiers."
    ),
    responses={200: RetourHistoriqueSerializer(many=True)},
)
class HistoriqueRetourView(generics.ListAPIView):
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if getattr(self.request.user, "role", None) in ROLES_ADMINISTRATION:
            return RetourHistoriqueAdministrationSerializer
        return RetourHistoriqueSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):  # génération du schéma OpenAPI
            return RetourHistorique.objects.none()
        demande = get_object_or_404(services.demandes_visibles(self.request.user), pk=self.kwargs["pk"])
        return RetourHistorique.objects.filter(demande_retour=demande)


@extend_schema(
    summary="Faire avancer une demande de retour",
    description=(
        "Vendeur : approuver ou rejeter (motif `reponse` obligatoire) une demande avant son échéance "
        "(`date_limite_vendeur`), rejeter sa propre approbation. Client : contester un refus avant "
        "`date_limite_contestation` (motif de 10 caractères au moins), en_transit, annuler avant l'expédition. "
        "Modérateur (dossier en_moderation) et administration (dossier en_arbitrage) : approuver ou rejeter, "
        "motif obligatoire. Vendeur et administration : en_transit, receptionner, rembourser, cloturer. "
        "Action qui ne revient pas à ce compte : 403 ; transition invalide ou délai écoulé : 400."
    ),
    request=TraiterDemandeRetourSerializer,
    responses={200: DemandeRetourSerializer, **erreurs(403, 404)},
)
class TraiterDemandeRetourView(APIView):
    """Actions sur une demande (table des transitions : apps.retours.services).

    Le vendeur décide, le client peut contester devant la modération puis
    devant l'administration ; vendeur et administration font avancer le
    colis et le remboursement.
    """

    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        serializer = TraiterDemandeRetourSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        donnees = serializer.validated_data
        try:
            services.traiter(pk, request.user, donnees["action"], donnees["reponse"], donnees["restock"])
        except services.RetourRefuse as refus:
            raise erreur_refus(refus)
        demande = services.demandes_visibles(request.user).get(pk=pk)
        return Response(DemandeRetourSerializer(demande, context={"request": request}).data, status=status.HTTP_200_OK)


@extend_schema(
    summary="Ajouter une photo justificative",
    description="Client de la demande, avant la réception, 5 photos au plus. Fichier manquant : 400 `errors.image`.",
    request={"multipart/form-data": PhotoRetourSerializer},
    responses={201: PhotoRetourSerializer, **erreurs(403, 404)},
)
class AjouterPhotoRetourView(APIView):
    """Photo justificative ajoutée par le client, tant que le retour n'est pas
    traité (5 au plus). Fichier vérifié (taille, contenu réel) et renommé."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "retour_photo"

    def post(self, request, pk):
        if "image" not in request.FILES:
            raise ValidationError({"image": ["Veuillez fournir un fichier image."]})
        with transaction.atomic():
            try:
                demande = services.verifier_ajout_photo(pk, request.user)
            except services.RetourRefuse as refus:
                raise erreur_refus(refus)
            # Le serializer applique les validateurs du modèle (signature
            # binaire réelle) : un objects.create() direct les contournerait.
            serializer = PhotoRetourSerializer(data=request.data, context={"request": request})
            serializer.is_valid(raise_exception=True)
            photo = serializer.save(demande_retour=demande)
        return Response(PhotoRetourSerializer(photo, context={"request": request}).data, status=status.HTTP_201_CREATED)


@extend_schema(
    summary="Télécharger une photo justificative",
    responses=FICHIER_IMAGE,
)
class TelechargerPhotoRetourView(APIView):
    """Fichier d'une photo justificative, servi par Django après contrôle
    d'accès (client, vendeur de la boutique, administration) : /media/
    n'est pas exposé en production."""

    permission_classes = [IsAuthenticated]

    def get(self, request, pk, photo_id):
        demande = get_object_or_404(services.demandes_visibles(request.user), pk=pk)
        photo = get_object_or_404(PhotoRetour, pk=photo_id, demande_retour=demande)
        try:
            contenu = photo.image.open("rb")
        except FileNotFoundError:
            logger_securite.error("Photo de retour référencée mais absente du stockage (photo_id=%s).", photo.pk)
            raise Http404("Ce fichier n'est plus disponible.")
        return FileResponse(contenu, filename=photo.image.name.rsplit("/", 1)[-1])
