from rest_framework import serializers
from .models import Commande, GroupeCommande, CommandeItem


class CommandeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Commande
        fields = [
            "id", "numero_commande","groupe", "boutique", "status",
            "client","montant_total", "created_at", "update_at"
            ]
        read_only_fields = fields


class GroupeCommandeSerializer(serializers.ModelSerializer):
    class Meta:
        model = GroupeCommande
        fields = [
            "id", "client", "created_at"
            ]
        read_only_fields = fields


class CommandeItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = CommandeItem
        fields = [
            "id", "commande", "variante", "nom_produit", "prix_unitaire","quantite"
        ]
        read_only_fields = fields