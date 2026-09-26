import uuid
from decimal import Decimal
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from rest_framework.throttling import ScopedRateThrottle

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
    """Échange des points de fidélité contre un bon de réduction personnalisé.

    SÉCURITÉ (A04:2025) : throttle_scope dédié plutôt que le taux générique
    'user' — c'est une opération financière réelle (débit de points, émission
    d'un coupon monnétisable), pas une simple consultation.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'fidelite_conversion'

    def post(self, request):
        serializer = ConvertirPointsCouponSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        regle = serializer.validated_data["_regle"]
        points_a_debiter = regle["points"]
        type_reduction = regle["type"]
        valeur = regle["valeur"]
        min_achat = regle["min"]

        code_coupon = f"FID-{uuid.uuid4().hex[:8].upper()}"
        expiration = timezone.now() + timedelta(days=90)  # Validité 3 mois

        with transaction.atomic():
            # Le solde a déjà été vérifié dans le serializer, mais sans
            # verrou : deux requêtes simultanées auraient pu lire le même
            # solde avant qu'aucune n'ait débité, et passer toutes les deux
            # la vérification — double conversion pour un seul solde
            # suffisant (A04/A08). On reverrouille et revérifie ici, juste
            # avant le débit réel, pour que la seconde requête concurrente
            # voie forcément le solde déjà réduit par la première.
            compte = CompteFidelite.objects.select_for_update().get(utilisateur=request.user)

            try:
                compte.debiter_points(
                    points=points_a_debiter,
                    description=f"Conversion de {points_a_debiter} pts en coupon {code_coupon}",
                    reference_externe=code_coupon,
                )
            except DjangoValidationError as exc:
                # Se produit si une requête concurrente a débité le solde
                # entre la vérification du serializer et ce verrou — la
                # seconde requête doit échouer proprement en 400, pas planter
                # en 500 (le double débit est bloqué ; il faut juste le
                # signaler clairement au client plutôt que par une erreur serveur).
                raise ValidationError({"solde_points": exc.messages if hasattr(exc, "messages") else str(exc)})

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
    """Vérifie la validité d'un code promo / coupon sur un montant de panier donné.

    SÉCURITÉ (A04:2025) : cette vue interroge CouponReduction par `code`,
    sans le restreindre au client courant (nécessaire : un coupon peut être
    public, non nominatif). Sans limite de débit dédiée, un compte
    authentifié pouvait soumettre jusqu'à 300 codes/heure (taux générique
    'user') pour tenter d'en découvrir par bourrinage — y compris des
    coupons nominatifs appartenant à d'autres clients, dont la réponse
    révèle l'existence ("Ce coupon est nominatif et ne vous appartient
    pas.") même s'il reste inutilisable. throttle_scope dédié en défense
    en profondeur, en plus du grand espace de codes générés
    (FID-XXXXXXXX, 32 bits) qui rend déjà le bourrinage peu praticable.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'coupon_verification'

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