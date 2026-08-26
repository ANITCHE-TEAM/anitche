from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny

from .models import Panier, PanierItem
from .serializers import PanierSerializer, PanierItemSerializer


def get_or_create_panier(request):
    """Récupère le panier de l'utilisateur connecté, ou celui du visiteur
    anonyme via sa session. Crée le panier s'il n'existe pas encore."""

    if request.user.is_authenticated:
        panier, _ = Panier.objects.get_or_create(utilisateur=request.user)
        return panier

    if not request.session.session_key:
        request.session.create()

    panier, _ = Panier.objects.get_or_create(
        session_key=request.session.session_key
    )
    return panier


class PanierDetailView(generics.RetrieveAPIView):
    permission_classes = [AllowAny]
    serializer_class = PanierSerializer

    def get_object(self):
        return get_or_create_panier(self.request)


class PanierItemListCreateView(generics.ListCreateAPIView):
    permission_classes = [AllowAny]
    serializer_class = PanierItemSerializer

    def get_queryset(self):
        panier = get_or_create_panier(self.request)
        return PanierItem.objects.filter(panier=panier).select_related('variante__produit', 'variante__stock')

    def perform_create(self, serializer):
        panier = get_or_create_panier(self.request)
        variante = serializer.validated_data.get('variante')
        quantite = serializer.validated_data.get('quantite', 1)

        # Quantité déjà présente pour cette variante, si elle existe déjà dans le panier
        existant = PanierItem.objects.filter(panier=panier, variante=variante).first()
        quantite_totale = quantite + (existant.quantite if existant else 0)

        stock = getattr(variante, 'stock', None)
        if stock is None or not stock.est_en_stock(quantite_totale):
            disponible = stock.quantite_disponible if stock else 0
            raise ValidationError(
                f"Stock insuffisant : {disponible} disponible(s), {quantite_totale} demandé(s)."
            )

        if existant:
            existant.quantite = quantite_totale
            existant.save(update_fields=['quantite'])
            serializer.instance = existant
            return

        serializer.save(panier=panier)


class PanierItemDetailView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [AllowAny]
    serializer_class = PanierItemSerializer

    def get_queryset(self):
        panier = get_or_create_panier(self.request)
        return PanierItem.objects.filter(panier=panier).select_related('variante__produit', 'variante__stock')

    def perform_update(self, serializer):
        # La quantité peut être modifiée via PATCH — même vérification de stock nécessaire.
        item = self.get_object()
        nouvelle_quantite = serializer.validated_data.get('quantite', item.quantite)
        stock = getattr(item.variante, 'stock', None)

        if stock is None or not stock.est_en_stock(nouvelle_quantite):
            disponible = stock.quantite_disponible if stock else 0
            raise ValidationError(
                f"Stock insuffisant : {disponible} disponible(s), {nouvelle_quantite} demandé(s)."
            )

        serializer.save()