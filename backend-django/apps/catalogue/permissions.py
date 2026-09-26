"""Contrôle d'appartenance de l'espace vendeur (défense en profondeur).

Les querysets des vues sont déjà filtrés sur le propriétaire (404 pour les
objets d'une autre boutique) ; ces permissions le revérifient au niveau de
l'objet. L'administration n'a pas accès à l'espace vendeur (EstVendeurValide) :
elle modère les produits via administration/produits/ (EstAdministrateur).
"""

from rest_framework.permissions import BasePermission


class EstProprietaireDuProduit(BasePermission):
    """Permission vérifiant que l'utilisateur est le propriétaire de la boutique vendant le produit."""

    message = "Vous n'avez pas la permission de modifier ce produit."

    def has_object_permission(self, request, view, obj):
        return obj.boutique.proprietaire_id == request.user.id


class EstProprietaireDeLaVariante(BasePermission):
    """Permission vérifiant que l'utilisateur est le propriétaire de la boutique vendant la variante."""

    message = "Vous n'avez pas la permission de modifier cette variante."

    def has_object_permission(self, request, view, obj):
        return obj.produit.boutique.proprietaire_id == request.user.id


class EstProprietaireDeLImage(BasePermission):
    """Permission vérifiant que l'utilisateur est le propriétaire de la boutique associée à l'image."""

    message = "Vous n'avez pas la permission de modifier ou supprimer cette image."

    def has_object_permission(self, request, view, obj):
        return obj.produit.boutique.proprietaire_id == request.user.id
