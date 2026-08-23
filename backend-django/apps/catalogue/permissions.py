from rest_framework.permissions import BasePermission
from apps.utilisateurs.models import Role


class EstProprietaireDuProduit(BasePermission):
    """Permission vérifiant que l'utilisateur est le propriétaire de la boutique vendant le produit."""

    message = "Vous n'avez pas la permission de modifier ce produit."

    def has_object_permission(self, request, view, obj):
        if request.user.role in [Role.ADMIN, Role.SUPER_ADMIN]:
            return True
        return obj.boutique.proprietaire == request.user


class EstProprietaireDeLaVariante(BasePermission):
    """Permission vérifiant que l'utilisateur est le propriétaire de la boutique vendant la variante."""

    message = "Vous n'avez pas la permission de modifier cette variante."

    def has_object_permission(self, request, view, obj):
        if request.user.role in [Role.ADMIN, Role.SUPER_ADMIN]:
            return True
        return obj.produit.boutique.proprietaire == request.user


class EstProprietaireDeLImage(BasePermission):
    """Permission vérifiant que l'utilisateur est le propriétaire de la boutique associée à l'image."""

    message = "Vous n'avez pas la permission de modifier ou supprimer cette image."

    def has_object_permission(self, request, view, obj):
        if request.user.role in [Role.ADMIN, Role.SUPER_ADMIN]:
            return True
        return obj.produit.boutique.proprietaire == request.user
