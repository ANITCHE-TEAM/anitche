from decimal import Decimal
import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from rest_framework.throttling import ScopedRateThrottle
from rest_framework import status

from .models import Commande, GroupeCommande, CommandeItem
from .serializers import CommandeSerializer, GroupeCommandeSerializer, CommandeItemSerializer
from apps.catalogue.models import Stock
from apps.panier.models import Panier
from apps.panier.views import get_or_create_panier

logger_securite = logging.getLogger('securite')


class ValiderPanierView(APIView):
    """Transforme le panier courant en une ou plusieurs commandes (une par
    boutique), décrémente le stock, puis vide le panier.

    Toute la logique tourne dans une transaction atomique : si une seule
    étape échoue (stock insuffisant, etc.), rien n'est enregistré.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'commande_validation'

    def post(self, request):
        panier = get_or_create_panier(request)

        with transaction.atomic():
            # Verrouille la ligne du panier lui-même avant toute lecture de
            # ses articles : une double soumission (double-clic, retry
            # réseau) envoie deux requêtes qui liraient sinon le même panier
            # en parallèle et créeraient chacune leur propre commande avec
            # les mêmes articles, avant que l'une des deux n'ait eu le temps
            # de le vider (A04/A08 — double-commande / double-facturation).
            # La seconde requête reste bloquée ici jusqu'à ce que la
            # première ait terminé (stock décrémenté, panier vidé, commit) ;
            # elle relit alors un panier vide et échoue proprement en 400.
            panier = Panier.objects.select_for_update().get(pk=panier.pk)

            items = list(
                panier.items.select_related("variante__produit__boutique", "variante__stock")
            )

            if not items:
                raise ValidationError("Le panier est vide.")

            # Une variante retirée de la vente entre l'ajout au panier et le
            # paiement ne doit pas pouvoir être commandée.
            variante_inactive = next((item for item in items if not item.variante.est_active), None)
            if variante_inactive is not None:
                raise ValidationError(
                    f"{variante_inactive.variante.nom} n'est plus disponible à la vente."
                )

            # 1. Regroupe les articles par boutique
            items_par_boutique = {}
            for item in items:
                boutique = item.variante.produit.boutique
                items_par_boutique.setdefault(boutique, []).append(item)

            # 2. Verrouille les lignes de stock concernées pour toute la durée
            # de la transaction : aucune autre commande ne peut décrémenter
            # ces mêmes variantes tant que celle-ci n'est pas terminée.
            variante_ids = [item.variante_id for item in items]
            stocks_verrouilles = {
                s.variante_id: s
                for s in Stock.objects.select_for_update().filter(variante_id__in=variante_ids)
            }

            for item in items:
                stock = stocks_verrouilles.get(item.variante_id)
                if stock is None or not stock.est_en_stock(item.quantite):
                    disponible = stock.quantite_disponible if stock else 0
                    raise ValidationError(
                        f"Stock insuffisant pour {item.variante.nom} : {disponible} disponible(s)."
                    )

            groupe = GroupeCommande.objects.create(client=request.user)
            commandes_creees = []

            for boutique, boutique_items in items_par_boutique.items():
                montant_total = sum(
                    (item.prix_unitaire * item.quantite for item in boutique_items),
                    Decimal("0.00")
                )

                commande = Commande.objects.create(
                    groupe=groupe,
                    boutique=boutique,
                    client=request.user,
                    montant_total=montant_total,
                )

                for item in boutique_items:
                    CommandeItem.objects.create(
                        commande=commande,
                        variante=item.variante,
                        nom_produit=item.variante.produit.nom,
                        prix_unitaire=item.prix_unitaire,
                        quantite=item.quantite,
                    )
                    try:
                        stocks_verrouilles[item.variante_id].decrementer(item.quantite)
                    except DjangoValidationError as exc:
                        # Filet de sécurité : normalement impossible grâce au
                        # verrou posé ci-dessus, mais on préfère un rollback +
                        # 400 propre à une erreur 500 si jamais ça se produit.
                        raise ValidationError(str(exc))

                commandes_creees.append(commande)

            # 3. Vide le panier une fois les commandes créées
            panier.items.all().delete()

        logger_securite.info(
            "Commande(s) créée(s) : groupe_id=%s, client_id=%s, nb_commandes=%s, montant_total=%s",
            groupe.id, request.user.id, len(commandes_creees),
            sum((c.montant_total for c in commandes_creees), Decimal("0.00")),
        )

        serializer = CommandeSerializer(commandes_creees, many=True)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class GroupeCommandeListView(generics.ListAPIView):
    """Liste les groupes de commandes du client connecté."""
    permission_classes = [IsAuthenticated]
    serializer_class = GroupeCommandeSerializer

    def get_queryset(self):
        return GroupeCommande.objects.filter(client=self.request.user)


class CommandeListView(generics.ListAPIView):
    """Liste les commandes du client connecté."""
    permission_classes = [IsAuthenticated]
    serializer_class = CommandeSerializer

    def get_queryset(self):
        return Commande.objects.filter(client=self.request.user)


class CommandeDetailView(generics.RetrieveAPIView):
    """Détail d'une commande précise, avec ses articles."""
    permission_classes = [IsAuthenticated]
    serializer_class = CommandeSerializer

    def get_queryset(self):
        return Commande.objects.filter(client=self.request.user)


class CommandeItemListView(generics.ListAPIView):
    """Liste les articles d'une commande précise (vérifie l'accès)."""
    permission_classes = [IsAuthenticated]
    serializer_class = CommandeItemSerializer

    def get_queryset(self):
        commande = get_object_or_404(
            Commande.objects.filter(client=self.request.user),
            pk=self.kwargs["commande_id"]
        )
        return CommandeItem.objects.filter(commande=commande)