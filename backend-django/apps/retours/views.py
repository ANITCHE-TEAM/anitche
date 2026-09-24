from django.shortcuts import get_object_or_404
from django.db import transaction
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination

from .models import DemandeRetour, RetourItem, PhotoRetour
from .serializers import (
    DemandeRetourSerializer,
    CreerDemandeRetourSerializer,
    PhotoRetourSerializer,
    TraiterDemandeRetourSerializer,
)
from .signals import retour_status_change
from apps.utilisateurs.models import Role
from apps.commandes.models import Commande



# Transitions d'état valides pour une demande de retour.
# Clé = statut de départ (valeurs réelles de DemandeRetour.Statut, pas des libellés
# inventés — un décalage ici fait échouer TOUTES les transitions silencieusement).
# Voir apps/retours/models.py::DemandeRetour.Statut pour la liste canonique.
TRANSITIONS_VALIDES = {
    DemandeRetour.Statut.DEMANDE: {"approuver", "rejeter"},
    DemandeRetour.Statut.APPROUVE: {"en_transit", "rejeter"},
    DemandeRetour.Statut.EN_TRANSIT: {"receptionner"},
    DemandeRetour.Statut.RECEPTIONNE: {"rembourser", "rejeter"},
    DemandeRetour.Statut.REMBOURSE: {"cloturer"},
    DemandeRetour.Statut.REJETE: {"cloturer"},
    DemandeRetour.Statut.CLOTURE: set(),  # état terminal, aucune transition possible
}

# Mapping action -> nouveau statut résultant (à adapter si tes noms diffèrent)
ACTION_VERS_STATUT = {
    "approuver": "approuvee",
    "rejeter": "rejetee",
    "en_transit": "en_transit",
    "receptionner": "receptionnee",
    "rembourser": "remboursee",
    "cloturer": "cloturee",
}


class DemandeRetourListCreateView(APIView):
    """Permet au client de lister ses demandes de retour ou d'en créer une nouvelle."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        retours = DemandeRetour.objects.filter(client=request.user).select_related("commande", "boutique").prefetch_related("articles__commande_item", "photos")
        # Pagination manuelle : cette vue est un APIView brut, pas un
        # ListAPIView, donc DEFAULT_PAGINATION_CLASS (config/settings/base.py)
        # ne s'applique jamais automatiquement ici (l'application automatique
        # de DRF passe par ListAPIView.list(), jamais par un Response()
        # construit à la main).
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(retours, request, view=self)
        serializer = DemandeRetourSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        commande_id = request.data.get("commande_id")

        with transaction.atomic():
            if commande_id:
                # Verrouille la ligne de la commande pour toute la durée de
                # la validation + création : sans ce verrou, deux requêtes
                # quasi simultanées pour le même article peuvent chacune
                # lire "quantité encore disponible" avant qu'aucune des deux
                # n'ait créé son RetourItem, et cumuler un remboursement/
                # restock supérieur à ce qui a été réellement acheté
                # (A04:2025 — race condition sur l'intégrité des données,
                # même famille que F-09 sur le stock).
                Commande.objects.select_for_update().filter(
                    id=commande_id, client=request.user
                ).first()

            serializer = CreerDemandeRetourSerializer(data=request.data, context={"request": request})
            serializer.is_valid(raise_exception=True)

            data = serializer.validated_data
            commande = data["_commande"]
            boutique = data["_boutique"]
            montant = data["_montant_remboursement"]
            items_a_creer = data["_validated_items"]

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

    def get_object(self, request, pk):
        """Ne renvoie la demande que si l'utilisateur est admin ou le
        vendeur propriétaire de la boutique concernée — sans ce filtre,
        n'importe quel compte authentifié pouvait traiter le retour
        d'un autre vendeur (A01:2025 — Broken Access Control)."""
        user = request.user
        if user.role in (Role.ADMIN, Role.SUPER_ADMIN):
            qs = DemandeRetour.objects.all()
        else:
            qs = DemandeRetour.objects.filter(boutique__proprietaire=user)
        return get_object_or_404(qs, pk=pk)

    def patch(self, request, *args, **kwargs):
        user = request.user
        demande = self.get_object(request, kwargs["pk"])
        action = request.data.get("action")

        if action not in ACTION_VERS_STATUT:
            return Response(
                {"detail": "Action inconnue."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        actions_autorisees = TRANSITIONS_VALIDES.get(demande.statut, set())
        if action not in actions_autorisees:
            return Response(
                {
                    "detail": (
                        f"Transition invalide : l'action '{action}' n'est pas autorisée "
                        f"depuis le statut '{demande.statut}'."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
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

        # SÉCURITÉ (A04/A08) : PhotoRetour.image porte bien
        # validators=[validateur_image_standard] (taille max, signature
        # binaire réelle du fichier), mais Model.objects.create() ne
        # déclenche JAMAIS ces validators — seuls full_clean() ou le
        # passage par un serializer DRF le font. Un .objects.create()
        # direct ici acceptait donc n'importe quel fichier, de n'importe
        # quelle taille, sans aucune vérification de contenu. On passe
        # désormais par PhotoRetourSerializer pour que la validation
        # s'applique réellement, comme partout ailleurs dans le module.
        serializer = PhotoRetourSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        photo = serializer.save(demande_retour=demande)

        return Response(PhotoRetourSerializer(photo).data, status=status.HTTP_201_CREATED)