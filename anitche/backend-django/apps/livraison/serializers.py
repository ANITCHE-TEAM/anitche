from rest_framework import serializers
from .models import Livraison, LivraisonHistorique


class LivraisonHistoriqueSerializer(serializers.ModelSerializer):
    class Meta:
        model = LivraisonHistorique
        fields = [
            "id", "livraison", "ancien_status", "nouveau_status",
            "effectue_par", "commentaire", "created_at"
        ]
        read_only_fields = fields


class LivraisonSerializer(serializers.ModelSerializer):
    class Meta:
        model = Livraison
        fields = [
            "id", "commande", "livreur", "status", "adresse_livraison",
            "date_expedition", "date_livraison", "created_at", "updated_at"
        ]
        read_only_fields = [
            "id", "commande", "date_expedition", "date_livraison",
            "created_at", "updated_at"
        ]


class LivraisonChangerStatusSerializer(serializers.Serializer):
    """Utilisé uniquement pour valider le payload de changement de statut."""
    status = serializers.ChoiceField(choices=Livraison.Status.choices)
    commentaire = serializers.CharField(required=False, allow_blank=True, max_length=255)