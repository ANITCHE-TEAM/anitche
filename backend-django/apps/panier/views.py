from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny

from .models import Panier, PanierItem
from .serializers import PanierSerializer, PanierItemSerializer


def get_panier_existant(request):
    """Cherche un panier existant SANS jamais en créer un nouveau.

    Utilisée pour toute lecture (GET) : consulter un panier ne doit
    jamais avoir d'effet de bord en base. Retourne None si aucun panier
    n'existe encore pour cet utilisateur/cette session.
    """
    if request.user.is_authenticated:
        return Panier.objects.filter(utilisateur=request.user).first()

    session_key = request.session.session_key
    if not session_key:
        return None
    return Panier.objects.filter(session_key=session_key).first()


def get_or_create_panier(request):
    """Récupère le panier de l'utilisateur connecté, ou celui du visiteur
    anonyme via sa session. Crée le panier s'il n'existe pas encore.

    Réservée aux actions d'écriture réelles (ajouter un article) : sans
    cette distinction, les vues en AllowAny créaient un nouveau Panier en
    base sur un simple GET, y compris pour un client qui ne conserve pas
    ses cookies (bot, script sans session persistée) — une ligne Panier
    orpheline à chaque requête, sans limite (A04:2025 — Unrestricted
    Resource Consumption).
    """

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
        panier = get_panier_existant(self.request)
        if panier is not None:
            return panier
        # Aucun panier n'existe encore : on en renvoie un vide, non
        # persisté, plutôt que d'en créer un juste pour cette consultation.
        utilisateur = self.request.user if self.request.user.is_authenticated else None
        return Panier(utilisateur=utilisateur)


class PanierItemListCreateView(generics.ListCreateAPIView):
    permission_classes = [AllowAny]
    serializer_class = PanierItemSerializer

    def get_queryset(self):
        panier = get_panier_existant(self.request)
        if panier is None:
            return PanierItem.objects.none()
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
        panier = get_panier_existant(self.request)
        if panier is None:
            return PanierItem.objects.none()
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