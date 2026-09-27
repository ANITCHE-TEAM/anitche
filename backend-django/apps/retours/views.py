import logging
import uuid

from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.exceptions import ErreurMetier
from apps.vendeurs.permissions import ROLES_ADMINISTRATION

from . import services
from .models import DemandeRetour, PhotoRetour
from .serializers import (
    CreerDemandeRetourSerializer,
    DemandeRetourSerializer,
    PhotoRetourSerializer,
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


class DemandeRetourListCreateView(APIView):
    """Le client liste ses demandes de retour ou en crée une (commande livrée,
    dans le délai de retour)."""

    permission_classes = [IsAuthenticated]

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
        # APIView brut : la pagination par défaut ne s'applique pas d'elle-même.
        paginator = PageNumberPagination()
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


class TraiterDemandeRetourView(APIView):
    """Actions sur une demande (table des transitions : apps.retours.services).

    Boutique (vendeur propriétaire ou administration) : approuver, rejeter
    (motif obligatoire), en_transit, receptionner, rembourser, cloturer.
    Client de la demande : en_transit (« j'ai expédié le colis »), annuler.
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
