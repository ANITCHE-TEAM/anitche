from rest_framework.permissions import BasePermission

from apps.utilisateurs.models import Role

#: Rôles qui modèrent les avis. Le modérateur n'a aucun autre pouvoir :
#: `EstAdministrateur` (apps.vendeurs.permissions) ne l'inclut pas.
ROLES_MODERATION_AVIS = (Role.MODERATEUR, Role.ADMIN, Role.SUPER_ADMIN)


class EstModerateurAvis(BasePermission):
    """Modération des avis : modérateur, admin et super_admin uniquement.

    `is_staff` n'est pas un critère (il ne sert qu'au Django admin)."""

    message = "Action réservée à la modération des avis."

    def has_permission(self, request, view):
        utilisateur = request.user
        return bool(
            utilisateur and utilisateur.is_authenticated and utilisateur.role in ROLES_MODERATION_AVIS
        )
