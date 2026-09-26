from django.db.models import Prefetch
from rest_framework import generics
from rest_framework.permissions import AllowAny

from .models import Panier, PanierItem
from .serializers import PanierSerializer, PanierItemSerializer
from .services import (
    ajouter_article,
    get_or_create_panier,
    get_panier_existant,
    modifier_quantite,
)


class PanierDetailView(generics.RetrieveAPIView):
    permission_classes = [AllowAny]
    serializer_class = PanierSerializer

    def get_object(self):
        # Lignes et tout ce que leur sérialisation lit, chargés en un nombre
        # fixe de requêtes quel que soit le nombre de lignes.
        paniers = Panier.objects.prefetch_related(
            Prefetch('items', queryset=PanierItem.objects.avec_details())
        )
        panier = get_panier_existant(self.request, queryset=paniers)
        if panier is not None:
            return panier
        # Aucun panier n'existe encore : on en renvoie un vide, non
        # persisté, plutôt que d'en créer un juste pour cette consultation.
        # id=None : l'id renvoyé est null tant que le panier n'existe pas,
        # au lieu d'un UUID inventé qui changeait à chaque appel.
        utilisateur = self.request.user if self.request.user.is_authenticated else None
        return Panier(id=None, utilisateur=utilisateur)


class PanierItemListCreateView(generics.ListCreateAPIView):
    permission_classes = [AllowAny]
    serializer_class = PanierItemSerializer

    def get_queryset(self):
        panier = get_panier_existant(self.request)
        if panier is None:
            return PanierItem.objects.none()
        return PanierItem.objects.filter(panier=panier).avec_details()

    def perform_create(self, serializer):
        panier = get_or_create_panier(self.request)
        serializer.instance = ajouter_article(
            panier,
            serializer.validated_data['variante'],
            serializer.validated_data.get('quantite', 1),
        )


class PanierItemDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [AllowAny]
    serializer_class = PanierItemSerializer

    def get_queryset(self):
        panier = get_panier_existant(self.request)
        if panier is None:
            return PanierItem.objects.none()
        return PanierItem.objects.filter(panier=panier).avec_details()

    def perform_update(self, serializer):
        # Seule la quantité est modifiable (variante figée, voir le serializer).
        if 'quantite' in serializer.validated_data:
            serializer.instance = modifier_quantite(
                serializer.instance, serializer.validated_data['quantite']
            )
