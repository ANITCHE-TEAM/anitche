import uuid
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from .models import CompteFidelite, TransactionFidelite, CouponReduction
from .serializers import (
    CompteFideliteSerializer,
    TransactionFideliteSerializer,
    CouponReductionSerializer,
    ConvertirPointsCouponSerializer,
    VerifierCouponSerializer,
)


class MonCompteFideliteView(APIView):
    """Consulter l'état de son compte fidélité (solde, palier, total cumulé)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        compte, _ = CompteFidelite.objects.get_or_create(utilisateur=request.user)
        return Response(CompteFideliteSerializer(compte).data, status=status.HTTP_200_OK)


class HistoriqueTransactionsFideliteView(generics.ListAPIView):
    """Historique des gains et dépenses de points de fidélité."""

    permission_classes = [IsAuthenticated]
    serializer_class = TransactionFideliteSerializer

    def get_queryset(self):
        return TransactionFidelite.objects.filter(compte__utilisateur=self.request.user)


class MesCouponsListView(generics.ListAPIView):
    """Liste des coupons et réductions actifs et passés de l'utilisateur."""

    permission_classes = [IsAuthenticated]
    serializer_class = CouponReductionSerializer

    def get_queryset(self):
        return CouponReduction.objects.filter(client=self.request.user)


class ConvertirPointsEnCouponView(APIView):
    """Échange des points de fidélité contre un bon de réduction personnalisé."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = ConvertirPointsCouponSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        compte = serializer.validated_data["_compte"]
        regle = serializer.validated_data["_regle"]

        points_a_debiter = regle["points"]
        type_reduction = regle["type"]
        valeur = regle["valeur"]
        min_achat = regle["min"]

        code_coupon = f"FID-{uuid.uuid4().hex[:8].upper()}"
        expiration = timezone.now() + timedelta(days=90)  # Validité 3 mois

        with transaction.atomic():
            compte.debiter_points(
                points=points_a_debiter,
                description=f"Conversion de {points_a_debiter} pts en coupon {code_coupon}",
                reference_externe=code_coupon,
            )

            coupon = CouponReduction.objects.create(
                code=code_coupon,
                client=request.user,
                type_reduction=type_reduction,
                valeur=valeur,
                montant_minimum_commande=min_achat,
                points_requis=points_a_debiter,
                date_expiration=expiration,
            )

        return Response(CouponReductionSerializer(coupon).data, status=status.HTTP_201_CREATED)


class VerifierCouponView(APIView):
    """Vérifie la validité d'un code promo / coupon sur un montant de panier donné."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = VerifierCouponSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        code = serializer.validated_data["code"].strip().upper()
        montant = serializer.validated_data["montant_commande"]

        coupon = CouponReduction.objects.filter(code__iexact=code).first()
        if not coupon:
            return Response(
                {"valide": False, "detail": f"Le code promo '{code}' n'existe pas."},
                status=status.HTTP_404_NOT_FOUND,
            )

        valide, message = coupon.est_valide_pour(request.user, montant)
        if not valide:
            return Response(
                {"valide": False, "detail": message},
                status=status.HTTP_400_BAD_REQUEST,
            )

        remise = coupon.calculer_remise(montant)
        nouveau_montant = max(Decimal("0.00"), montant - remise)

        return Response(
            {
                "valide": True,
                "detail": "Coupon appliqué avec succès.",
                "remise": str(remise),
                "montant_initial": str(montant),
                "montant_final": str(nouveau_montant),
                "coupon": CouponReductionSerializer(coupon).data,
            },
            status=status.HTTP_200_OK,
        )
