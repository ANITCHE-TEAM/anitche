"""Permissions de l'espace vendeur des passeports.

Mêmes règles que les routes ma-boutique/ du module vendeurs : les
permissions de apps.vendeurs.permissions restent la source de vérité.
"""

from rest_framework.permissions import BasePermission

from apps.vendeurs.permissions import (  # noqa: F401 (BoutiqueDuVendeurNonSuspendue réexportée)
    BoutiqueDuVendeurNonSuspendue,
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

