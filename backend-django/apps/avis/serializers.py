from rest_framework import serializers

from . import services
from .models import Avis

NOTE_MIN = 1
NOTE_MAX = 5
COMMENTAIRE_MAX = 2000


class CreerAvisSerializer(serializers.Serializer):
    """Corps de la publication d'un avis."""

    commande_item_id = serializers.UUIDField(help_text="Ligne de la commande livrée (article acheté) à noter.")
    note = serializers.IntegerField(min_value=NOTE_MIN, max_value=NOTE_MAX)
    commentaire = serializers.CharField(
        required=False, allow_blank=True, max_length=COMMENTAIRE_MAX, default="",
        help_text="Facultatif : la note seule suffit.",
    )


class ModifierAvisSerializer(serializers.Serializer):
    """Corps de la modification : au moins un des deux champs."""

    note = serializers.IntegerField(min_value=NOTE_MIN, max_value=NOTE_MAX, required=False)
    commentaire = serializers.CharField(required=False, allow_blank=True, max_length=COMMENTAIRE_MAX)

    def validate(self, donnees):
        if not donnees:
            raise serializers.ValidationError("Indiquez la note ou le commentaire à modifier.")
        return donnees


class AvisSerializer(serializers.ModelSerializer):
    """Un avis tel que son auteur le voit."""

    produit_id = serializers.IntegerField(read_only=True)
    produit_nom = serializers.CharField(source="produit.nom", read_only=True)
    produit_slug = serializers.CharField(source="produit.slug", read_only=True)
    boutique_nom = serializers.CharField(source="boutique.nom", read_only=True)
    commande_item_id = serializers.UUIDField(read_only=True)
    modifiable_jusqu_au = serializers.SerializerMethodField(
        help_text="Fin du délai de modification par l'auteur (ISO 8601).",
    )

    class Meta:
        model = Avis
        fields = [
            "id", "produit_id", "produit_nom", "produit_slug", "boutique_nom", "commande_item_id",
            "note", "commentaire", "statut", "est_modifie", "date_creation", "date_modification",
            "modifiable_jusqu_au",
        ]
        read_only_fields = fields

    def get_modifiable_jusqu_au(self, avis) -> str:
        return services.date_limite_modification(avis).isoformat()


class ArticleANoterSerializer(serializers.Serializer):
    """Une ligne de commande livrée que le client peut encore noter."""

    commande_item_id = serializers.UUIDField(source="id")
    numero_commande = serializers.CharField(source="commande.numero_commande")
    produit_id = serializers.IntegerField(source="variante.produit_id")
    produit_nom = serializers.CharField(source="variante.produit.nom")
    produit_slug = serializers.CharField(source="variante.produit.slug")
    boutique_nom = serializers.CharField(source="commande.boutique.nom")
    date_livraison = serializers.DateTimeField(source="commande.livraison.date_livraison")
    avis_possible_jusqu_au = serializers.SerializerMethodField()

    def get_avis_possible_jusqu_au(self, article) -> str:
        return services.date_limite_avis(article.commande).isoformat()
