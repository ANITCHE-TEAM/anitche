from rest_framework import serializers
from .models import Panier, PanierItem


class PanierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Panier
        fields = "__all__"

        read_only_fields = [
            "id","utilisateur", "created_at", "updated_at"
        ]


class PanierItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = PanierItem
        fields = "__all__"

        read_only_fields = [
            "id","added_at","panier"
        ]
        