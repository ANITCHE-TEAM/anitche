"""Permissions de l'espace vendeur des passeports.

Mêmes règles que les routes ma-boutique/ du module vendeurs : les
permissions de apps.vendeurs.permissions restent la source de vérité.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.vendeurs.models import Boutique
from apps.vendeurs.permissions import (
    ROLES_ADMINISTRATION,
    BoutiqueNonSuspendue,
    EstAdministrateur,
    EstVendeurValide,
)


class EstVendeurValideOuAdministrateur(BasePermission):
    """`EstVendeurValide | EstAdministrateur`, avec un message en français.

    La composition `|` de DRF renverrait le message générique anglais de DRF.
    """

    message = EstVendeurValide.message

    def has_permission(self, request, view):
        return (
            EstVendeurValide().has_permission(request, view)
            or EstAdministrateur().has_permission(request, view)
        )


class BoutiqueDuVendeurNonSuspendue(BasePermission):
    """Boutique suspendue : lecture autorisée, toute écriture refusée (403).

    Transposition de `BoutiqueNonSuspendue` : le vendeur n'agit que sur les
    passeports de sa propre boutique (queryset filtré), c'est donc elle qui
    est vérifiée. L'administration n'est pas concernée (elle doit pouvoir
    révoquer un passeport d'une boutique suspendue).
    """

    message = BoutiqueNonSuspendue.message

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS or request.user.role in ROLES_ADMINISTRATION:
            return True
        return not Boutique.objects.filter(proprietaire_id=request.user.pk, est_suspendue=True).exists()
