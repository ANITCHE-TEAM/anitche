from django.shortcuts import get_object_or_404
from django.db import transaction
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from .models import DemandeRetour, RetourItem, PhotoRetour
from .serializers import (
    DemandeRetourSerializer,
    CreerDemandeRetourSerializer,
    PhotoRetourSerializer,
    TraiterDemandeRetourSerializer,
)
from .signals import retour_status_change
from apps.utilisateurs.models import Role


class DemandeRetourListCreateView(APIView):
    """Permet au client de lister ses demandes de retour ou d'en créer une nouvelle."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        retours = DemandeRetour.objects.filter(client=request.user).select_related("commande", "boutique").prefetch_related("articles__commande_item", "photos")
        serializer = DemandeRetourSerializer(retours, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def post(self, request):
        serializer = CreerDemandeRetourSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        data = serializer.validated_data
        commande = data["_commande"]
        boutique = data["_boutique"]
        montant = data["_montant_remboursement"]
        items_a_creer = data["_validated_items"]

        with transaction.atomic():
            demande = DemandeRetour.objects.create(
                commande=commande,
                client=request.user,
                boutique=boutique,
                motif=data["motif"],
                type_resolution=data["type_resolution"],
                description=data["description"],
                montant_remboursement=montant,
                statut=DemandeRetour.Statut.DEMANDE,
            )

            for commande_item, qte in items_a_creer:
                RetourItem.objects.create(
                    demande_retour=demande,
                    commande_item=commande_item,
                    quantite=qte,
                )

        return Response(DemandeRetourSerializer(demande).data, status=status.HTTP_201_CREATED)


class DemandeRetourDetailView(generics.RetrieveAPIView):
    """Détail d'une demande de retour précise."""

    permission_classes = [IsAuthenticated]
    serializer_class = DemandeRetourSerializer

    def get_queryset(self):
        user = self.request.user
        qs = DemandeRetour.objects.select_related("commande", "boutique", "client").prefetch_related("articles__commande_item", "photos")

        if user.role in (Role.ADMIN, Role.SUPER_ADMIN):
            return qs
        if user.role == Role.VENDEUR:
            return qs.filter(boutique__proprietaire=user)
        return qs.filter(client=user)


class EspaceVendeurRetoursListView(generics.ListAPIView):
    """Liste des demandes de retour concernant la boutique du vendeur connecté."""

    permission_classes = [IsAuthenticated]
    serializer_class = DemandeRetourSerializer

    def get_queryset(self):
        user = self.request.user
        if user.role in (Role.ADMIN, Role.SUPER_ADMIN):
            return DemandeRetour.objects.select_related("commande", "boutique", "client").prefetch_related("articles__commande_item", "photos")
        return DemandeRetour.objects.filter(boutique__proprietaire=user).select_related("commande", "boutique", "client").prefetch_related("articles__commande_item", "photos")


class TraiterDemandeRetourView(APIView):
    """Action de traitement d'un retour (approuver, rejeter, réceptionner, rembourser) par le vendeur ou admin."""

    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        demande = get_object_or_404(DemandeRetour, pk=pk)
        user = request.user

        est_vendeur_concerne = getattr(demande.boutique, "proprietaire_id", None) == user.id
        est_admin = user.role in (Role.ADMIN, Role.SUPER_ADMIN)

        if not (est_vendeur_concerne or est_admin):
            return Response(
                {"detail": "Vous n'êtes pas autorisé à traiter cette demande de retour."},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = TraiterDemandeRetourSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        action = serializer.validated_data["action"]
        reponse = serializer.validated_data.get("reponse", "")
        restock = serializer.validated_data.get("restock", True)

        ancien_statut = demande.statut

        if action == "approuver":
            demande.approuver(reponse=reponse, effectue_par=user)
        elif action == "rejeter":
            demande.rejeter(motif_refus=reponse, effectue_par=user)
        elif action == "en_transit":
            demande.statut = DemandeRetour.Statut.EN_TRANSIT
            demande.save(update_fields=["statut", "date_mise_a_jour"])
        elif action == "receptionner":
            demande.receptionner(restock=restock)
        elif action == "rembourser":
            demande.statut = DemandeRetour.Statut.REMBOURSE
            demande.save(update_fields=["statut", "date_mise_a_jour"])
        elif action == "cloturer":
            demande.statut = DemandeRetour.Statut.CLOTURE
            demande.date_cloture = timezone.now()
            demande.save(update_fields=["statut", "date_cloture", "date_mise_a_jour"])

        retour_status_change.send(
            sender=DemandeRetour,
            demande_retour=demande,
            ancien_statut=ancien_statut,
            nouveau_statut=demande.statut,
        )

        return Response(DemandeRetourSerializer(demande).data, status=status.HTTP_200_OK)


class AjouterPhotoRetourView(APIView):
    """Upload d'une photo justificative pour une demande de retour."""

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        demande = get_object_or_404(DemandeRetour, pk=pk, client=request.user)

        if "image" not in request.FILES:
            return Response({"image": "Veuillez fournir un fichier image."}, status=status.HTTP_400_BAD_REQUEST)

        photo = PhotoRetour.objects.create(
            demande_retour=demande,
            image=request.FILES["image"],
        )

        return Response(PhotoRetourSerializer(photo).data, status=status.HTTP_201_CREATED)
