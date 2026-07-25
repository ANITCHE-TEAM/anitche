"""Permissions du domaine vendeur.

`EstVendeurValide` est volontairement exposée ici pour être réutilisée par les
autres modules (catalogue, commandes, retours...) : c'est le garde-fou unique
qui empêche un vendeur non validé de publier ou de vendre.
"""

from rest_framework.permissions import BasePermission

from apps.utilisateurs.models import Role, StatutKYC

#: Rôles considérés comme « administration ANITCHE ».
ROLES_ADMINISTRATION = (Role.ADMIN, Role.SUPER_ADMIN)


class EstAdministrateur(BasePermission):
    """Réservé au back-office : rôle admin/super_admin, ou compte staff Django."""

    message = "Action réservée à l'administration ANITCHE."

    def has_permission(self, request, view):
        utilisateur = request.user
        return bool(
            utilisateur
            and utilisateur.is_authenticated
            and (utilisateur.is_staff or utilisateur.role in ROLES_ADMINISTRATION)
        )


class EstVendeurValide(BasePermission):
    """Compte vendeur dont le KYC est validé.

    `statut_kyc` reste la source de vérité : le rôle seul ne suffit pas.
    """

    message = "Votre compte vendeur doit être validé avant cette action."

    def has_permission(self, request, view):
        utilisateur = request.user
        return bool(
            utilisateur
            and utilisateur.is_authenticated
            and utilisateur.role == Role.VENDEUR
            and utilisateur.statut_kyc == StatutKYC.VALIDE
        )


class EstProprietaireDeLaBoutique(BasePermission):
    """Seul le propriétaire agit sur sa boutique."""

    message = "Vous n'êtes pas le propriétaire de cette boutique."

    def has_object_permission(self, request, view, obj):
        return obj.proprietaire_id == request.user.id
