from rest_framework import serializers

from . import services
from .models import Avis, DecisionModeration, ReponseVendeur, SignalementAvis

NOTE_MIN = 1
NOTE_MAX = 5
COMMENTAIRE_MAX = 2000
SIGNALEMENT_COMMENTAIRE_MAX = 500
MOTIF_MODERATION_MAX = 1000


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


class ReponseVendeurSerializer(serializers.ModelSerializer):
    """Réponse de la boutique à un avis."""

    class Meta:
        model = ReponseVendeur
        fields = ["contenu", "date_creation", "date_modification"]
        read_only_fields = fields


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
    reponse_vendeur = ReponseVendeurSerializer(source="reponse", read_only=True, allow_null=True)

    class Meta:
        model = Avis
        fields = [
            "id", "produit_id", "produit_nom", "produit_slug", "boutique_nom", "commande_item_id",
            "note", "commentaire", "statut", "est_modifie", "date_creation", "date_modification",
            "modifiable_jusqu_au", "reponse_vendeur",
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


# =====================================================================
# RÉPONSE DU VENDEUR
# =====================================================================

class ContenuReponseSerializer(serializers.Serializer):
    """Corps de la réponse du vendeur (création et modification)."""

    contenu = serializers.CharField(max_length=COMMENTAIRE_MAX)


class AvisPublicSerializer(serializers.ModelSerializer):
    """Un avis publié tel que le public le voit : prénom et initiale du nom
    seulement, jamais l'email ni l'identifiant du client."""

    auteur = serializers.SerializerMethodField()
    reponse_vendeur = ReponseVendeurSerializer(source="reponse", read_only=True, allow_null=True)

    class Meta:
        model = Avis
        fields = ["id", "auteur", "note", "commentaire", "est_modifie", "date_creation", "reponse_vendeur"]
        read_only_fields = fields

    def get_auteur(self, avis) -> str:
        initiale = f" {avis.client.nom[:1]}." if avis.client.nom else ""
        return f"{avis.client.prenom}{initiale}".strip()


class AvisRecuSerializer(AvisPublicSerializer):
    """Un avis reçu par la boutique, vu par son propriétaire."""

    produit_id = serializers.IntegerField(read_only=True)
    produit_nom = serializers.CharField(source="produit.nom", read_only=True)

    class Meta(AvisPublicSerializer.Meta):
        fields = ["id", "produit_id", "produit_nom", *AvisPublicSerializer.Meta.fields[1:]]
        read_only_fields = fields


# =====================================================================
# SIGNALEMENT ET MODÉRATION
# =====================================================================

class SignalerAvisSerializer(serializers.Serializer):
    """Corps du signalement : motif parmi une liste fermée."""

    motif = serializers.ChoiceField(choices=SignalementAvis.Motif.choices)
    commentaire = serializers.CharField(
        required=False, allow_blank=True, max_length=SIGNALEMENT_COMMENTAIRE_MAX, default="",
        help_text="Facultatif : précision pour la modération.",
    )


class SignalementSerializer(serializers.ModelSerializer):
    """Signalement tel que son auteur le reçoit."""

    avis_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = SignalementAvis
        fields = ["id", "avis_id", "motif", "commentaire", "statut", "date_creation"]
        read_only_fields = fields


class MotifModerationSerializer(serializers.Serializer):
    """Corps d'une décision de modération : le motif est obligatoire."""

    motif = serializers.CharField(max_length=MOTIF_MODERATION_MAX)


class AvisModerationSerializer(serializers.ModelSerializer):
    """Avis signalé, tel que la modération le lit."""

    produit_id = serializers.IntegerField(read_only=True)
    produit_nom = serializers.CharField(source="produit.nom", read_only=True)
    boutique_nom = serializers.CharField(source="boutique.nom", read_only=True)
    client_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = Avis
        fields = [
            "id", "produit_id", "produit_nom", "boutique_nom", "client_id", "note", "commentaire", "statut",
        ]
        read_only_fields = fields


class SignalementModerationSerializer(serializers.ModelSerializer):
    """Signalement dans la file de modération."""

    avis = AvisModerationSerializer(read_only=True)
    signalant_id = serializers.IntegerField(source="auteur_id", read_only=True)

    class Meta:
        model = SignalementAvis
        fields = [
            "id", "avis", "signalant_id", "motif", "commentaire", "statut", "date_creation", "date_traitement",
        ]
        read_only_fields = fields


class DecisionModerationSerializer(serializers.ModelSerializer):
    """Décision de modération : qui, quand, quoi, pourquoi."""

    avis_id = serializers.UUIDField(read_only=True)
    moderateur_id = serializers.IntegerField(read_only=True)

    class Meta:
        model = DecisionModeration
        fields = ["id", "avis_id", "moderateur_id", "type_decision", "motif", "date_creation"]
        read_only_fields = fields


class FiltreSignalementsSerializer(serializers.Serializer):
    """Paramètre de requête de la file de modération."""

    statut = serializers.ChoiceField(
        choices=SignalementAvis.Statut.choices, required=False, default=SignalementAvis.Statut.OUVERT,
        help_text="Défaut `ouvert`. `accepte` retrouve les avis masqués à la suite d'un signalement.",
    )
