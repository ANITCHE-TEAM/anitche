"""Permissions du domaine vendeur.

`EstVendeurValide` est volontairement exposée ici pour être réutilisée par les
autres modules (catalogue, commandes, retours...) : c'est le garde-fou unique
qui empêche un vendeur non validé de publier ou de vendre.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from apps.utilisateurs.models import Role, StatutKYC

from .models import Boutique

#: Rôles considérés comme « administration ANITCHE ».
ROLES_ADMINISTRATION = (Role.ADMIN, Role.SUPER_ADMIN)


class EstAdministrateur(BasePermission):
    """Réservé au back-office : rôle admin/super_admin uniquement.

    `is_staff` n'est volontairement pas un critère alternatif : il ne
    doit donner accès qu'au Django admin, pas aux pouvoirs métier de
    cette API (validation KYC, suspension de boutique...). Un compte
    staff « technique » sans rôle admin ne doit pas hériter de ces
    pouvoirs par accident.
    """

    message = "Action réservée à l'administration ANITCHE."

    def has_permission(self, request, view):
        utilisateur = request.user
        return bool(
            utilisateur
            and utilisateur.is_authenticated
            and utilisateur.role in ROLES_ADMINISTRATION
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


class BoutiqueNonSuspendue(BasePermission):
    """Une boutique suspendue par l'administration est gelée côté vendeur.

    Lecture seule : le vendeur voit sa boutique (et `est_suspendue`), mais
    toute écriture est refusée (403) tant que la suspension n'est pas
    levée — y compris la fermeture/réouverture volontaire (`est_active`).
    """

    message = (
        "Votre boutique est suspendue par l'administration : elle reste "
        "consultable, mais aucune modification n'est possible tant que la "
        "suspension n'est pas levée."
    )

    def has_object_permission(self, request, view, obj):
        return request.method in SAFE_METHODS or not obj.est_suspendue


class BoutiqueDuVendeurNonSuspendue(BasePermission):
    """Boutique suspendue : lecture autorisée, toute écriture refusée (403).

    Transposition de `BoutiqueNonSuspendue` pour les modules qui gèrent des
    objets de la boutique (catalogue, passeports) : le vendeur n'agit que sur
    ses propres objets (querysets filtrés), c'est donc SA boutique qui est
    vérifiée. L'administration n'est pas concernée (elle doit pouvoir
    modérer les objets d'une boutique suspendue).
    """

    message = BoutiqueNonSuspendue.message

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS or request.user.role in ROLES_ADMINISTRATION:
            return True
        return not Boutique.objects.filter(proprietaire_id=request.user.pk, est_suspendue=True).exists()
