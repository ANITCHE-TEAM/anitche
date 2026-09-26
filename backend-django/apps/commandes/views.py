from decimal import ROUND_DOWN, Decimal
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
from .serializers import (
    AdresseLivraisonSerializer,
    CommandeDetailSerializer,
    CommandeSerializer,
    CommandeVendeurSerializer,
    GroupeCommandeSerializer,
    CommandeItemSerializer,
)
from .services import TransitionImpossible, annuler_commande, est_payee, passer_en_preparation
from apps.catalogue.models import Stock
from apps.panier.models import Panier
from apps.panier.services import get_or_create_panier
from apps.fidelite.models import CouponReduction
from apps.utilisateurs.permissions import EmailVerifie
from apps.vendeurs.permissions import BoutiqueNonSuspendue, EstAdministrateur, EstVendeurValide

logger_securite = logging.getLogger('securite')


class ValiderPanierView(APIView):
    """Transforme le panier courant en une ou plusieurs commandes (une par
    boutique), décrémente le stock, puis vide le panier.

    Toute la logique tourne dans une transaction atomique : si une seule
    étape échoue (stock insuffisant, etc.), rien n'est enregistré.
    """
    # Email vérifié obligatoire pour commander (apps.utilisateurs.permissions).
    permission_classes = [IsAuthenticated, EmailVerifie]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'commande_validation'

    def post(self, request):
        # Adresse de livraison obligatoire, validée avant toute écriture.
        adresse = AdresseLivraisonSerializer(data=request.data.get("adresse_livraison"))
        if request.data.get("adresse_livraison") is None:
            raise ValidationError({"adresse_livraison": "L'adresse de livraison est obligatoire."})
        adresse.is_valid(raise_exception=True)
        adresse = adresse.validated_data

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

            # F-10 : verrouille la ligne du coupon (s'il y en a un) en même
            # temps que le panier. Sans ce verrou, une double soumission
            # pourrait faire lire "est_utilise=False" par les deux requêtes
            # avant qu'aucune ne l'ait encore posé à True — le même coupon
            # serait alors appliqué deux fois (double dépense, comme pour les
            # points de fidélité, voir CompteFidelite.debiter_points).
            coupon_code_saisi = str(request.data.get("coupon_code", "")).strip()
            coupon = None
            if coupon_code_saisi:
                coupon = CouponReduction.objects.select_for_update().filter(
                    code__iexact=coupon_code_saisi
                ).first()
                if coupon is None:
                    raise ValidationError(
                        {"coupon_code": f"Le code promo '{coupon_code_saisi}' n'existe pas."}
                    )

            items = list(panier.items.avec_details())

            if not items:
                raise ValidationError("Le panier est vide.")

            # Une variante retirée de la vente, un produit désactivé, ou une
            # boutique suspendue (KYC révoqué, boutique désactivée, vendeur
            # banni) entre l'ajout au panier et le paiement ne doivent jamais
            # pouvoir être commandés. Produit.est_achetable est le point
            # d'entrée unique documenté pour cette règle (voir
            # Boutique.est_publiable) : PanierItem.est_disponible l'applique
            # (avec variante.est_active), la même règle que l'API panier (F-13).
            # Toutes les lignes concernées sont listées, pas seulement la
            # première, pour que le client les retire en une fois.
            items_non_achetables = [item for item in items if not item.est_disponible]
            if items_non_achetables:
                libelles = ", ".join(
                    f"{item.variante.produit.nom} ({item.variante.nom})"
                    for item in items_non_achetables
                )
                raise ValidationError(
                    f"Ces articles ne sont plus disponibles à la vente : {libelles}. "
                    "Retirez-les du panier pour valider la commande."
                )

            # 1. Regroupe les articles par boutique
            items_par_boutique = {}
            for item in items:
                boutique = item.variante.produit.boutique
                items_par_boutique.setdefault(boutique, []).append(item)

            montants_par_boutique = {
                boutique: sum(
                    (item.prix_unitaire * item.quantite for item in boutique_items),
                    Decimal("0.00"),
                )
                for boutique, boutique_items in items_par_boutique.items()
            }
            montant_total_panier = sum(montants_par_boutique.values(), Decimal("0.00"))

            # F-10 : un coupon s'applique au panier entier, qui peut couvrir
            # plusieurs boutiques. On calcule ici la remise globale puis on la
            # répartit au prorata du montant de chaque boutique, plutôt que
            # de la porter en entier par une seule commande arbitraire.
            remises_par_boutique = {boutique: Decimal("0.00") for boutique in items_par_boutique}
            if coupon is not None:
                valide, message = coupon.est_valide_pour(request.user, montant_total_panier)
                if not valide:
                    raise ValidationError({"coupon_code": message})

                montant_remise_total = coupon.calculer_remise(montant_total_panier)

                # Parts en francs entiers (FCFA) : arrondies au franc
                # inférieur, le dernier lot absorbe le reste pour que la
                # somme des parts égale exactement la remise du panier.
                boutiques = list(items_par_boutique.keys())
                remise_cumulee = Decimal("0")
                for index, boutique in enumerate(boutiques):
                    if index == len(boutiques) - 1:
                        part = montant_remise_total - remise_cumulee
                    else:
                        part = (
                            montant_remise_total * montants_par_boutique[boutique] / montant_total_panier
                        ).quantize(Decimal("1"), rounding=ROUND_DOWN)
                        remise_cumulee += part
                    remises_par_boutique[boutique] = part

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

            groupe = GroupeCommande.objects.create(
                client=request.user,
                livraison_commune=adresse["commune"],
                livraison_quartier=adresse["quartier"],
                livraison_point_de_repere=adresse["point_de_repere"],
                livraison_telephone=adresse["telephone"],
            )
            commandes_creees = []

            for boutique, boutique_items in items_par_boutique.items():
                montant_boutique = montants_par_boutique[boutique]
                remise_boutique = remises_par_boutique[boutique]

                commande = Commande.objects.create(
                    groupe=groupe,
                    boutique=boutique,
                    client=request.user,
                    montant_total=montant_boutique - remise_boutique,
                    coupon_code=coupon.code if coupon is not None else "",
                    montant_remise=remise_boutique,
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

            # F-10 : le coupon n'est marqué utilisé qu'une fois toutes les
            # commandes effectivement créées (dans la même transaction) —
            # grâce au verrou posé plus haut, aucune autre requête n'a pu le
            # consommer entre-temps.
            if coupon is not None:
                coupon.est_utilise = True
                coupon.save(update_fields=["est_utilise"])

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
    """Détail d'une commande précise, avec ses articles et l'adresse."""
    permission_classes = [IsAuthenticated]
    serializer_class = CommandeDetailSerializer

    def get_queryset(self):
        return Commande.objects.filter(client=self.request.user).select_related("groupe").prefetch_related("article")


def reponse_transition_impossible(erreur):
    return Response({"detail": str(erreur)}, status=status.HTTP_409_CONFLICT)


class AnnulerCommandeView(APIView):
    """Annulation par le client, tant que la commande n'est pas en
    préparation. Stock restitué une seule fois ; un paiement déjà encaissé
    passe « à rembourser »."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        commande = get_object_or_404(Commande, pk=pk, client=request.user)
        try:
            annuler_commande(commande, Commande.MotifAnnulation.CLIENT)
        except TransitionImpossible as erreur:
            return reponse_transition_impossible(erreur)
        commande = Commande.objects.select_related("groupe").prefetch_related("article").get(pk=commande.pk)
        return Response(CommandeDetailSerializer(commande).data, status=status.HTTP_200_OK)


# =====================================================================
# ESPACE VENDEUR
# =====================================================================

def commandes_du_vendeur(utilisateur):
    return Commande.objects.filter(boutique__proprietaire=utilisateur).select_related(
        "client", "groupe", "boutique",
    ).prefetch_related("article")


class CommandeVendeurListView(generics.ListAPIView):
    """Commandes de la boutique du vendeur connecté (une commande = une
    seule boutique : jamais les articles d'un autre vendeur)."""
    permission_classes = [IsAuthenticated, EstVendeurValide]
    serializer_class = CommandeVendeurSerializer

    def get_queryset(self):
        return commandes_du_vendeur(self.request.user)


class CommandeVendeurDetailView(generics.RetrieveAPIView):
    permission_classes = [IsAuthenticated, EstVendeurValide]
    serializer_class = CommandeVendeurSerializer

    def get_queryset(self):
        return commandes_du_vendeur(self.request.user)


class PasserEnPreparationView(APIView):
    """confirmee → preparation, par le vendeur de la boutique.

    Boutique suspendue : seules les commandes déjà payées peuvent encore
    être honorées (403 sinon).
    """
    permission_classes = [IsAuthenticated, EstVendeurValide]

    def post(self, request, pk):
        commande = get_object_or_404(commandes_du_vendeur(request.user), pk=pk)
        if commande.boutique.est_suspendue and not est_payee(commande):
            return Response({"detail": BoutiqueNonSuspendue.message}, status=status.HTTP_403_FORBIDDEN)
        try:
            passer_en_preparation(commande)
        except TransitionImpossible as erreur:
            return reponse_transition_impossible(erreur)
        commande = commandes_du_vendeur(request.user).get(pk=commande.pk)
        return Response(CommandeVendeurSerializer(commande).data, status=status.HTTP_200_OK)


# =====================================================================
# ADMINISTRATION
# =====================================================================

class AnnulerCommandeAdministrationView(APIView):
    """Annulation par l'administration (jusqu'à la préparation incluse)."""
    permission_classes = [IsAuthenticated, EstAdministrateur]

    def post(self, request, pk):
        commande = get_object_or_404(Commande, pk=pk)
        try:
            annuler_commande(commande, Commande.MotifAnnulation.ADMINISTRATION)
        except TransitionImpossible as erreur:
            return reponse_transition_impossible(erreur)
        logger_securite.info(
            "Commande %s annulée par admin_id=%s", commande.numero_commande, request.user.id,
        )
        commande = Commande.objects.select_related("groupe").prefetch_related("article").get(pk=commande.pk)
        return Response(CommandeDetailSerializer(commande).data, status=status.HTTP_200_OK)


class CommandeItemListView(generics.ListAPIView):
    """Liste les articles d'une commande précise (vérifie l'accès)."""
    permission_classes = [IsAuthenticated]
    serializer_class = CommandeItemSerializer

    def get_queryset(self):
        commande = get_object_or_404(
            Commande.objects.filter(client=self.request.user),
            pk=self.kwargs["commande_id"]
        )
        # CommandeItem n'a pas de champ date — tri par id (UUID) pour un
        # ordre stable et déterministe entre les pages (sans ça, DRF émet
        # UnorderedObjectListWarning : la pagination sur un queryset non
        # trié peut sauter ou répéter des lignes d'une page à l'autre).
        return CommandeItem.objects.filter(commande=commande).order_by("id")