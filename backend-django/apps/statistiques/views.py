from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.vendeurs.models import Boutique
from apps.vendeurs.permissions import EstAdministrateur, EstVendeurValide
from config.schema import erreurs

from .serializers import (
    FiltrePeriodeSerializer,
    PeriodeSerializer,
    StatistiquesAdministrationSerializer,
    StatistiquesVendeurSerializer,
    lire_periode,
)
from .services import statistiques_administration, statistiques_vendeur

DESCRIPTION_PERIODE = (
    "`date_debut` et `date_fin` (AAAA-MM-JJ) sont inclusives, en jours entiers Africa/Abidjan ; sans elles, les 30 "
    "derniers jours. Valeur invalide, `date_fin` avant `date_debut` ou période de plus de 366 jours : 400. "
    "Une commande compte si elle est payée et non annulée ; elle se rattache à la période par sa date de création. "
    "Montants en entiers XOF, calculés depuis les montants figés dans les commandes."
)


@extend_schema(
    summary="Statistiques de ma boutique",
    description=(
        "Chiffre d'affaires, commandes par statut, panier moyen, top 5 des produits, note et nombre d'avis, taux de "
        "retour, montants reversés et à venir. Toujours la boutique du compte connecté. " + DESCRIPTION_PERIODE
    ),
    parameters=[FiltrePeriodeSerializer],
    responses={200: StatistiquesVendeurSerializer, **erreurs(400, 401, 403, 404)},
)
class StatistiquesVendeurView(APIView):
    permission_classes = [IsAuthenticated, EstVendeurValide]

    def get(self, request):
        periode = lire_periode(request.query_params)
        boutique = Boutique.objects.filter(proprietaire=request.user).first()
        if boutique is None:
            raise NotFound("Aucune boutique pour ce compte.")
        donnees = statistiques_vendeur(boutique, periode)
        return Response(StatistiquesVendeurSerializer({**donnees, 'periode': PeriodeSerializer.depuis(periode)}).data)


@extend_schema(
    summary="Statistiques de la plateforme (administration)",
    description=(
        "Volume des ventes, commissions ANITCHE, commandes par statut, nouveaux clients, vendeurs actifs, reversements "
        "en cours et versés, avis signalés en attente (état actuel, sans filtre de période). " + DESCRIPTION_PERIODE
    ),
    parameters=[FiltrePeriodeSerializer],
    responses={200: StatistiquesAdministrationSerializer, **erreurs(400, 401, 403)},
)
class StatistiquesAdministrationView(APIView):
    permission_classes = [IsAuthenticated, EstAdministrateur]

    def get(self, request):
        periode = lire_periode(request.query_params)
        donnees = statistiques_administration(periode)
        return Response(
            StatistiquesAdministrationSerializer({**donnees, 'periode': PeriodeSerializer.depuis(periode)}).data
        )
