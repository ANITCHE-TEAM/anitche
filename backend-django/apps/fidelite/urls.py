from django.urls import path
from .views import (
    MonCompteFideliteView,
    HistoriqueTransactionsFideliteView,
    MesCouponsListView,
    ConvertirPointsEnCouponView,
    VerifierCouponView,
)

app_name = "fidelite"

urlpatterns = [
    path("mon-compte/", MonCompteFideliteView.as_view(), name="fidelite-mon-compte"),
    path("transactions/", HistoriqueTransactionsFideliteView.as_view(), name="fidelite-transactions"),
    path("mes-coupons/", MesCouponsListView.as_view(), name="fidelite-mes-coupons"),
    path("convertir-points/", ConvertirPointsEnCouponView.as_view(), name="fidelite-convertir"),
    path("verifier-coupon/", VerifierCouponView.as_view(), name="fidelite-verifier-coupon"),
]
