import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import generics
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
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
    SimulationCheckoutSerializer,
    SimulerFraisSerializer,
)
from .services import (
    CheckoutRefuse,
    TransitionImpossible,
    annuler_commande,
    calculer_checkout,
    est_payee,
    passer_en_preparation,
    trouver_coupon,
)
from apps.catalogue.models import Stock
from apps.core.exceptions import ErreurMetier
from drf_spectacular.utils import extend_schema

from config.schema import erreurs
from apps.panier.models import Panier
from apps.panier.services import get_or_create_panier
from apps.fidelite.services import consommer_coupon
from apps.livraison.frais import TarifIntrouvable
from apps.notifications.services import alerter_stock_bas
from apps.paiements.frais import BaremeIntrouvable, bareme_en_vigueur, calculer_frais_ligne
from apps.utilisateurs.permissions import EmailVerifie
from apps.vendeurs.permissions import BoutiqueNonSuspendue, EstAdministrateur, EstVendeurValide

logger_securite = logging.getLogger('securite')


class ServiceIndisponible(APIException):
    status_code = 503
    default_detail = "La validation des commandes est momentanément indisponible."


@extend_schema(
    summary="Valider le panier (checkout)",
    description=(
        "Crée une commande par boutique (statut `creee`, à payer sous 30 minutes), décrémente le stock "
        "et vide le panier. Mêmes montants que `simuler-frais/`. Stock insuffisant ou panier vide : "
        "400 (`errors.non_field_errors`)."
    ),
    request=SimulerFraisSerializer,
    responses={201: CommandeSerializer(many=True), **erreurs(503)},
)
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

            # F-10 : le coupon (s'il y en a un) est verrouillé en même temps
            # que le panier : une double soumission ne peut pas l'appliquer
            # deux fois (voir CompteFidelite.debiter_points).
            try:
                coupon = trouver_coupon(request.data.get("coupon_code"), verrouiller=True)
                # Montants par boutique (remise répartie, frais de livraison
                # par commande) : même calcul que la simulation.
                checkout = calculer_checkout(list(panier.items.avec_details()), adresse, request.user, coupon)
            except CheckoutRefuse as refus:
                raise ValidationError(refus.detail)
            except TarifIntrouvable:
                logger_securite.error("Validation de panier impossible : aucun tarif de livraison en vigueur.")
                raise ServiceIndisponible()

            # 2. Verrouille les lignes de stock concernées pour toute la durée
            # de la transaction : aucune autre commande ne peut décrémenter
            # ces mêmes variantes tant que celle-ci n'est pas terminée.
            items = [item for lot in checkout.lots for item in lot.items]
            stocks_verrouilles = {
                s.variante_id: s
                for s in Stock.objects.select_for_update().filter(variante_id__in=[item.variante_id for item in items])
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
                livraison_zone=checkout.zone,
                livraison_commune=adresse["commune"],
                livraison_quartier=adresse["quartier"],
                livraison_point_de_repere=adresse["point_de_repere"],
                livraison_telephone=adresse["telephone"],
            )
            commandes_creees = []

            for lot in checkout.lots:
                # Frais vendeur en vigueur (offre de la boutique, sinon
                # plateforme), figés dans chaque article. Calculés sur le prix
                # avant remise : le coupon est supporté par ANITCHE.
                try:
                    bareme = bareme_en_vigueur(lot.boutique)
                except BaremeIntrouvable:
                    logger_securite.error("Validation de panier impossible : aucun barème de frais en vigueur.")
                    raise ServiceIndisponible()

                commande = Commande.objects.create(
                    groupe=groupe,
                    boutique=lot.boutique,
                    client=request.user,
                    montant_total=lot.montant_total,
                    coupon_code=coupon.code if coupon is not None else "",
                    montant_remise=lot.remise,
                    frais_livraison=lot.frais_livraison,
                    livraison_offerte=lot.livraison_offerte,
                    frais_livraison_vendeur=lot.frais_livraison_vendeur,
                )

                for item in lot.items:
                    CommandeItem.objects.create(
                        commande=commande,
                        variante=item.variante,
                        nom_produit=item.variante.produit.nom,
                        prix_unitaire=item.prix_unitaire,
                        quantite=item.quantite,
                        **calculer_frais_ligne(item.prix_unitaire, item.quantite, bareme),
                    )
                    stock = stocks_verrouilles[item.variante_id]
                    avant = stock.quantite_disponible
                    try:
                        stock.decrementer(item.quantite)
                    except DjangoValidationError as exc:
                        # Filet de sécurité : normalement impossible grâce au
                        # verrou posé ci-dessus, mais on préfère un rollback +
                        # 400 propre à une erreur 500 si jamais ça se produit.
                        raise ValidationError(str(exc))
                    # Franchissement du seuil d'alerte : le vendeur est prévenu (une fois).
                    alerter_stock_bas(stock, avant, avant - item.quantite)

                commandes_creees.append(commande)

            # F-10 : le coupon n'est marqué utilisé qu'une fois toutes les
            # commandes effectivement créées (dans la même transaction) —
            # grâce au verrou posé plus haut, aucune autre requête n'a pu le
            # consommer entre-temps.
            if coupon is not None:
                consommer_coupon(coupon, request.user, groupe)

            # 3. Vide le panier une fois les commandes créées
            panier.items.all().delete()

        logger_securite.info(
            "Commande(s) créée(s) : groupe_id=%s, client_id=%s, nb_commandes=%s, montant_total=%s",
            groupe.id, request.user.id, len(commandes_creees), checkout.total_a_payer,
        )

        serializer = CommandeSerializer(commandes_creees, many=True)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


@extend_schema(
    summary="Simuler les montants du checkout",
    description="Aucune écriture : montants par commande (une par boutique) et totaux, recalculés à l'identique à la validation.",
    request=SimulerFraisSerializer,
    responses={200: SimulationCheckoutSerializer, **erreurs(503)},
)
class SimulerFraisView(APIView):
    """Montants du checkout AVANT validation : par commande (une par
    boutique), articles, remise, frais de livraison (ou livraison offerte)
    et total, puis les totaux à payer. Aucune écriture, aucune réservation
    de stock : le montant réel est recalculé (identique) à la validation.

    Limite dédiée : sans elle, la simulation permettrait d'essayer des codes
    promo sans passer par la limite de la validation.
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'commande_simulation'

    def post(self, request):
        entree = SimulerFraisSerializer(data=request.data)
        entree.is_valid(raise_exception=True)
        panier = get_or_create_panier(request)
        try:
            coupon = trouver_coupon(entree.validated_data.get("coupon_code"))
            checkout = calculer_checkout(
                list(panier.items.avec_details()), entree.validated_data["adresse_livraison"], request.user, coupon,
            )
        except CheckoutRefuse as refus:
            raise ValidationError(refus.detail)
        except TarifIntrouvable:
            logger_securite.error("Simulation du checkout impossible : aucun tarif de livraison en vigueur.")
            raise ServiceIndisponible()

        return Response({
            "zone": checkout.zone,
            "commandes": [
                {
                    "boutique": lot.boutique.pk,
                    "boutique_nom": lot.boutique.nom,
                    "montant_articles": int(lot.montant_articles),
                    "remise": int(lot.remise),
                    "frais_livraison": int(lot.frais_livraison),
                    "livraison_offerte": lot.livraison_offerte,
                    "montant_total": int(lot.montant_total),
                }
                for lot in checkout.lots
            ],
            "total_articles": int(checkout.total_articles),
            "total_remise": int(checkout.total_remise),
            "total_frais_livraison": int(checkout.total_frais_livraison),
            "total_a_payer": int(checkout.total_a_payer),
        })


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


@extend_schema(
    summary="Annuler sa commande",
    description="Possible tant que la commande n'est pas en préparation (409 sinon).",
    request=None,
    responses={200: CommandeDetailSerializer, **erreurs(409)},
)
class AnnulerCommandeView(APIView):
    """Annulation par le client, tant que la commande n'est pas en
    préparation. Stock restitué une seule fois ; un paiement déjà encaissé
    passe « à rembourser »."""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        commande = get_object_or_404(Commande, pk=pk, client=request.user)
        try:
            annuler_commande(commande, Commande.MotifAnnulation.CLIENT, acteur=request.user)
        except TransitionImpossible as erreur:
            raise ErreurMetier(str(erreur), status.HTTP_409_CONFLICT)
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


@extend_schema(
    summary="Passer une commande en préparation (vendeur)",
    description="`confirmee` → `preparation`. Boutique suspendue : seules les commandes payées (403 sinon).",
    request=None,
    responses={200: CommandeVendeurSerializer, **erreurs(409)},
)
class PasserEnPreparationView(APIView):
    """confirmee → preparation, par le vendeur de la boutique.

    Boutique suspendue : seules les commandes déjà payées peuvent encore
    être honorées (403 sinon).
    """
    permission_classes = [IsAuthenticated, EstVendeurValide]

    def post(self, request, pk):
        commande = get_object_or_404(commandes_du_vendeur(request.user), pk=pk)
        if commande.boutique.est_suspendue and not est_payee(commande):
            raise PermissionDenied(BoutiqueNonSuspendue.message)
        try:
            passer_en_preparation(commande)
        except TransitionImpossible as erreur:
            raise ErreurMetier(str(erreur), status.HTTP_409_CONFLICT)
        commande = commandes_du_vendeur(request.user).get(pk=commande.pk)
        return Response(CommandeVendeurSerializer(commande).data, status=status.HTTP_200_OK)


# =====================================================================
# ADMINISTRATION
# =====================================================================

@extend_schema(
    summary="Annuler une commande (administration)",
    description="Jusqu'à la préparation incluse (409 au-delà).",
    request=None,
    responses={200: CommandeDetailSerializer, **erreurs(409)},
)
class AnnulerCommandeAdministrationView(APIView):
    """Annulation par l'administration (jusqu'à la préparation incluse)."""
    permission_classes = [IsAuthenticated, EstAdministrateur]

    def post(self, request, pk):
        commande = get_object_or_404(Commande, pk=pk)
        try:
            annuler_commande(commande, Commande.MotifAnnulation.ADMINISTRATION, acteur=request.user)
        except TransitionImpossible as erreur:
            raise ErreurMetier(str(erreur), status.HTTP_409_CONFLICT)
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