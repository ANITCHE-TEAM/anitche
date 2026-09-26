"""Permissions du module livraison.

Les rôles décident de l'accès à un endpoint ; le contrôle fin (livreur
assigné, client de la commande, propriétaire de la boutique) se fait sous
verrou dans apps.livraison.services, ou par le queryset de chaque vue.
`is_staff` ne donne aucun pouvoir ici (voir vendeurs.permissions) ;
l'administration et les vendeurs utilisent EstAdministrateur et
EstVendeurValide de apps.vendeurs.permissions.
"""

from rest_framework.permissions import BasePermission

from apps.utilisateurs.models import Role
from apps.vendeurs.permissions import ROLES_ADMINISTRATION


class EstLivreurOuAdministrateur(BasePermission):
    message = "Vous n'êtes pas autorisé à modifier cette livraison."

    def has_permission(self, request, view):
        utilisateur = request.user
        return bool(
            utilisateur and utilisateur.is_authenticated
            and (utilisateur.role == Role.LIVREUR or utilisateur.role in ROLES_ADMINISTRATION)
        )
