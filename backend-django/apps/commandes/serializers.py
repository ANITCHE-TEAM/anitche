import re

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Commande, GroupeCommande, CommandeItem


class AdresseLivraisonSerializer(serializers.Serializer):
    """Adresse saisie à la validation du panier, obligatoire.

    Structurée pour la Côte d'Ivoire : commune, quartier et point de repère
    (les adresses postales y sont rares, le livreur se guide aux repères),
    plus un téléphone joignable pour cette livraison, choisi par le client
    (visible par le vendeur et le livreur). La commune fixe le tarif de
    livraison. La zone n'est jamais demandée au client : le serveur la
    déduit de la commune (apps.livraison.frais) ; un champ `zone` envoyé est
    ignoré, sinon un client paierait le tarif d'Abidjan pour l'intérieur.
    """

    commune = serializers.CharField(max_length=100)
    quartier = serializers.CharField(max_length=150)
    point_de_repere = serializers.CharField(max_length=500)
    telephone = serializers.CharField(max_length=20)

    def validate_telephone(self, telephone):
        compact = re.sub(r"[\s.\-]", "", telephone)
        if not re.fullmatch(r"\+?\d{8,15}", compact):
            raise serializers.ValidationError("Numéro de téléphone invalide (8 à 15 chiffres, « + » initial accepté).")
        return compact


class AdresseLivraisonLectureSerializer(serializers.Serializer):
    """Adresse de livraison telle que renvoyée (schéma OpenAPI) : celle du
    checkout, avec la zone tarifaire déduite de la commune."""

    zone = serializers.CharField()
    commune = serializers.CharField()
    quartier = serializers.CharField()
    point_de_repere = serializers.CharField()
    telephone = serializers.CharField()


def adresse_du_groupe(groupe):
    if groupe is None or not groupe.a_une_adresse:
        return None
    return {
        "zone": groupe.livraison_zone,
        "commune": groupe.livraison_commune,
        "quartier": groupe.livraison_quartier,
        "point_de_repere": groupe.livraison_point_de_repere,
        "telephone": groupe.livraison_telephone,
    }


class CommandeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Commande
        fields = [
            "id", "numero_commande","groupe", "boutique", "status", "motif_annulation",
            "client","montant_total", "coupon_code", "montant_remise",
            "frais_livraison", "livraison_offerte", "created_at", "update_at"
            ]
        read_only_fields = fields


class GroupeCommandeSerializer(serializers.ModelSerializer):
    adresse_livraison = serializers.SerializerMethodField()

    class Meta:
        model = GroupeCommande
        fields = [
            "id", "client", "adresse_livraison", "created_at"
            ]
        read_only_fields = fields

    @extend_schema_field(AdresseLivraisonLectureSerializer(allow_null=True))
    def get_adresse_livraison(self, groupe):
        return adresse_du_groupe(groupe)


class CommandeItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = CommandeItem
        fields = [
            "id", "commande", "variante", "nom_produit", "prix_unitaire","quantite"
        ]
        read_only_fields = fields


class CommandeDetailSerializer(CommandeSerializer):
    """Détail pour le client : la commande, ses articles et l'adresse."""

    articles = CommandeItemSerializer(source="article", many=True, read_only=True)
    adresse_livraison = serializers.SerializerMethodField()

    class Meta(CommandeSerializer.Meta):
        fields = CommandeSerializer.Meta.fields + ["articles", "adresse_livraison"]
        read_only_fields = fields

    @extend_schema_field(AdresseLivraisonLectureSerializer(allow_null=True))
    def get_adresse_livraison(self, commande):
        return adresse_du_groupe(commande.groupe)


class ArticleVendeurSerializer(serializers.ModelSerializer):
    class Meta:
        model = CommandeItem
        fields = ["id", "variante", "nom_produit", "prix_unitaire", "quantite"]
        read_only_fields = fields


class CommandeVendeurSerializer(serializers.ModelSerializer):
    """Commande vue par le vendeur : le strict nécessaire pour la préparer
    et l'expédier. Jamais l'email ni le téléphone du profil du client (seul
    le téléphone de livraison, choisi par le client pour cette commande)."""

    articles = ArticleVendeurSerializer(source="article", many=True, read_only=True)
    client = serializers.SerializerMethodField()
    adresse_livraison = serializers.SerializerMethodField()

    class Meta:
        model = Commande
        fields = [
            "id", "numero_commande", "created_at", "status", "motif_annulation",
            "montant_total", "montant_remise", "frais_livraison", "livraison_offerte",
            "frais_livraison_vendeur", "articles", "client", "adresse_livraison",
        ]
        read_only_fields = fields

    def get_client(self, commande) -> str:
        """Prénom + initiale du nom (ex. « Awa K. »)."""
        client = commande.client
        initiale = f" {client.nom[:1].upper()}." if client.nom else ""
        return f"{client.prenom}{initiale}".strip()

    @extend_schema_field(AdresseLivraisonLectureSerializer(allow_null=True))
    def get_adresse_livraison(self, commande):
        return adresse_du_groupe(commande.groupe)


class SimulerFraisSerializer(serializers.Serializer):
    """Entrée de la simulation du checkout (mêmes champs que la validation)."""

    adresse_livraison = AdresseLivraisonSerializer()
    coupon_code = serializers.CharField(max_length=30, required=False, allow_blank=True)


# ---------------------------------------------------------------------
# Documentation OpenAPI de la simulation du checkout (réponse construite
# dans SimulerFraisView).
# ---------------------------------------------------------------------

class SimulationCommandeSerializer(serializers.Serializer):
    boutique = serializers.IntegerField()
    boutique_nom = serializers.CharField()
    montant_articles = serializers.IntegerField()
    remise = serializers.IntegerField()
    frais_livraison = serializers.IntegerField()
    livraison_offerte = serializers.BooleanField()
    montant_total = serializers.IntegerField()


class SimulationCheckoutSerializer(serializers.Serializer):
    zone = serializers.CharField(help_text="Zone tarifaire déduite de la commune.")
    commandes = SimulationCommandeSerializer(many=True, help_text="Une commande par boutique.")
    total_articles = serializers.IntegerField()
    total_remise = serializers.IntegerField()
    total_frais_livraison = serializers.IntegerField()
    total_a_payer = serializers.IntegerField()
