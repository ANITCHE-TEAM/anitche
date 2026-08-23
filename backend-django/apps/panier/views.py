from rest_framework import generics
from rest_framework.permissions import AllowAny

from .models import Panier, PanierItem
from .serializers import PanierSerializer, PanierItemSerializer


def get_or_create_panier(request):
    """Récupère le panier de l'utilisateur connecté, ou celui du visiteur
    anonyme via sa session. Crée le panier s'il n'existe pas encore."""

    if request.user.is_authenticated:
        panier, _ = Panier.objects.get_or_create(utilisateur=request.user)
        return panier

    # Un visiteur anonyme n'a de session_key que si une session a déjà été créée.
    if not request.session.session_key:
        request.session.create()

    panier, _ = Panier.objects.get_or_create(
        session_key=request.session.session_key
    )
    return panier


class PanierDetailView(generics.RetrieveAPIView):
    """Récupère le panier courant (utilisateur connecté ou visiteur anonyme).
    Créé automatiquement au premier appel s'il n'existe pas encore."""
    permission_classes = [AllowAny]
    serializer_class = PanierSerializer

    def get_object(self):
        return get_or_create_panier(self.request)


class PanierItemListCreateView(generics.ListCreateAPIView):
    """Liste les articles du panier courant (GET) et permet d'en ajouter un
    nouveau (POST)."""
    permission_classes = [AllowAny]
    serializer_class = PanierItemSerializer

    def get_queryset(self):
        panier = get_or_create_panier(self.request)
        return PanierItem.objects.filter(panier=panier).select_related('variante__produit', 'variante__stock')

    def perform_create(self, serializer):
        panier = get_or_create_panier(self.request)
        variante = serializer.validated_data.get('variante')
        quantite = serializer.validated_data.get('quantite', 1)

        if variante:
            existant = PanierItem.objects.filter(panier=panier, variante=variante).first()
            if existant:
                existant.quantite += quantite
                existant.save(update_fields=['quantite'])
                serializer.instance = existant
                return

        serializer.save(panier=panier)


class PanierItemDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Modifie la quantité ou supprime un article précis du panier courant."""
    permission_classes = [AllowAny]
    serializer_class = PanierItemSerializer

    def get_queryset(self):
        # Sécurité : un utilisateur ne peut modifier/supprimer que les items
        # de SON PROPRE panier, jamais ceux d'un autre visiteur/utilisateur.
        panier = get_or_create_panier(self.request)
        return PanierItem.objects.filter(panier=panier).select_related('variante__produit', 'variante__stock')