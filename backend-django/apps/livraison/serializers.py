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
    # Téléphone choisi par le client pour cette livraison, au checkout
    # (GroupeCommande) — jamais le téléphone de son profil.
    telephone_contact = serializers.CharField(
        source="commande.groupe.livraison_telephone", read_only=True, default="",
    )

    class Meta:
        model = Livraison
        fields = [
            "id", "commande", "livreur", "status", "adresse_livraison", "telephone_contact",
            "date_expedition", "date_livraison", "created_at", "updated_at"
        ]
        # `livreur` et `status` ne doivent jamais être modifiés par écriture
        # directe de serializer : le changement de statut passe uniquement
        # par Livraison.changer_status(), qui historise et déclenche le
        # signal métier. Les laisser modifiables ici serait une porte de
        # contournement si une vue d'update générique était ajoutée plus tard.
        read_only_fields = [
            "id", "commande", "livreur", "status", "date_expedition",
            "date_livraison", "created_at", "updated_at"
        ]


class LivraisonChangerStatusSerializer(serializers.Serializer):
    """Utilisé uniquement pour valider le payload de changement de statut."""
    status = serializers.ChoiceField(choices=Livraison.Status.choices)
    commentaire = serializers.CharField(required=False, allow_blank=True, max_length=255)