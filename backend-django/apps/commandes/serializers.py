import math
import re
from decimal import ROUND_HALF_UP, Decimal

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Commande, GroupeCommande, CommandeItem

# Point de livraison : territoire de la Côte d'Ivoire avec une marge
# (environ 4,3 à 10,7° N et 8,6 à 2,5° O).
LATITUDE_MIN, LATITUDE_MAX = 4.0, 11.0
LONGITUDE_MIN, LONGITUDE_MAX = -9.0, -2.0
# 6 décimales : environ 11 cm, la précision des colonnes de GroupeCommande.
PRECISION_COORDONNEE = Decimal("0.000001")


class CoordonneeGpsField(serializers.FloatField):
    """Latitude ou longitude en degrés décimaux (celles de
    navigator.geolocation) : un nombre JSON, jamais une chaîne ni un
    booléen, fini, entre min_value et max_value. Renvoie un Decimal arrondi
    à 6 décimales."""

    default_error_messages = {
        "invalid": "Un nombre est attendu (ex. 5.359952), pas une chaîne.",
        "non_fini": "Valeur non finie refusée (NaN, Infinity).",
        "min_value": "Position hors de la Côte d'Ivoire : valeur minimale {min_value}.",
        "max_value": "Position hors de la Côte d'Ivoire : valeur maximale {max_value}.",
    }

    def to_internal_value(self, data):
        # bool est un int en Python : true n'est pas une coordonnée.
        if isinstance(data, bool) or not isinstance(data, (int, float)):
            self.fail("invalid")
        if isinstance(data, float) and not math.isfinite(data):
            self.fail("non_fini")
        # str() garde la valeur écrite dans le JSON (5.3, pas 5.2999…).
        valeur = Decimal(str(data))
        # Bornes contrôlées avant l'arrondi : quantize() échouerait sur un
        # entier démesuré (1e40). Les validateurs de FloatField, qui portent
        # les bornes dans le schéma OpenAPI, repassent ensuite sans effet.
        if valeur < self.min_value:
            self.fail("min_value", min_value=self.min_value)
        if valeur > self.max_value:
            self.fail("max_value", max_value=self.max_value)
        return valeur.quantize(PRECISION_COORDONNEE, rounding=ROUND_HALF_UP)


class AdresseLivraisonSerializer(serializers.Serializer):
    """Adresse saisie à la validation du panier, obligatoire.

    Structurée pour la Côte d'Ivoire : commune, quartier et point de repère
    (les adresses postales y sont rares, le livreur se guide aux repères),
    plus un téléphone joignable pour cette livraison, choisi par le client
    (visible par le vendeur et le livreur). La commune fixe le tarif de
    livraison. La zone n'est jamais demandée au client : le serveur la
    déduit de la commune (apps.livraison.frais) ; un champ `zone` envoyé est
    ignoré, sinon un client paierait le tarif d'Abidjan pour l'intérieur.

    `latitude` et `longitude` : point GPS facultatif (bouton « ma position »),
    les deux ou aucune. Il ne change pas le tarif, qui dépend de la commune.
    """

    commune = serializers.CharField(max_length=100)
    quartier = serializers.CharField(max_length=150)
    point_de_repere = serializers.CharField(max_length=500)
    telephone = serializers.CharField(max_length=20)
    latitude = CoordonneeGpsField(
        min_value=LATITUDE_MIN, max_value=LATITUDE_MAX, required=False, allow_null=True,
        help_text="Facultatif, avec `longitude`. Degrés décimaux, arrondis à 6 décimales.",
    )
    longitude = CoordonneeGpsField(
        min_value=LONGITUDE_MIN, max_value=LONGITUDE_MAX, required=False, allow_null=True,
        help_text="Facultatif, avec `latitude`. Degrés décimaux, arrondis à 6 décimales.",
    )

    def validate_telephone(self, telephone):
        compact = re.sub(r"[\s.\-]", "", telephone)
        if not re.fullmatch(r"\+?\d{8,15}", compact):
            raise serializers.ValidationError("Numéro de téléphone invalide (8 à 15 chiffres, « + » initial accepté).")
        return compact

    def validate(self, attrs):
        latitude, longitude = attrs.get("latitude"), attrs.get("longitude")
        if (latitude is None) != (longitude is None):
            manquante = "longitude" if longitude is None else "latitude"
            raise serializers.ValidationError(
                {manquante: "La latitude et la longitude vont ensemble : envoyez les deux, ou aucune."}
            )
        return attrs


class AdresseLivraisonLectureSerializer(serializers.Serializer):
    """Adresse de livraison telle que renvoyée au vendeur (schéma OpenAPI) :
    celle du checkout, avec la zone tarifaire déduite de la commune, sans
    point GPS."""

    zone = serializers.CharField()
    commune = serializers.CharField()
    quartier = serializers.CharField()
    point_de_repere = serializers.CharField()
    telephone = serializers.CharField()


class AdresseLivraisonAvecPositionLectureSerializer(AdresseLivraisonLectureSerializer):
    """Adresse renvoyée au client, au livreur assigné et à l'administration :
    avec le point GPS, `null` quand le client ne l'a pas donné."""

    latitude = serializers.FloatField(allow_null=True)
    longitude = serializers.FloatField(allow_null=True)


def _degres(valeur):
    return None if valeur is None else float(valeur)


def adresse_du_groupe(groupe, avec_position=False):
    """Adresse du checkout, ou None pour un groupe sans adresse.

    `avec_position` ajoute le point GPS (nombres, ou null). Réservé au client
    de la commande, au livreur assigné (masqué avec toute l'adresse une fois
    la livraison terminée) et à l'administration : jamais au vendeur, pour
    qui l'adresse texte suffit à préparer le colis.
    """
    if groupe is None or not groupe.a_une_adresse:
        return None
    adresse = {
        "zone": groupe.livraison_zone,
        "commune": groupe.livraison_commune,
        "quartier": groupe.livraison_quartier,
        "point_de_repere": groupe.livraison_point_de_repere,
        "telephone": groupe.livraison_telephone,
    }
    if avec_position:
        adresse["latitude"] = _degres(groupe.livraison_latitude)
        adresse["longitude"] = _degres(groupe.livraison_longitude)
    return adresse


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
    """Groupes du client connecté : son adresse, point GPS compris."""

    adresse_livraison = serializers.SerializerMethodField()

    class Meta:
        model = GroupeCommande
        fields = [
            "id", "client", "adresse_livraison", "created_at"
            ]
        read_only_fields = fields

    @extend_schema_field(AdresseLivraisonAvecPositionLectureSerializer(allow_null=True))
    def get_adresse_livraison(self, groupe):
        return adresse_du_groupe(groupe, avec_position=True)


class CommandeItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = CommandeItem
        fields = [
            "id", "commande", "variante", "nom_produit", "prix_unitaire","quantite"
        ]
        read_only_fields = fields


class CommandeDetailSerializer(CommandeSerializer):
    """Détail pour le client (et réponse des annulations, client ou
    administration) : la commande, ses articles et l'adresse, point GPS
    compris."""

    articles = CommandeItemSerializer(source="article", many=True, read_only=True)
    adresse_livraison = serializers.SerializerMethodField()

    class Meta(CommandeSerializer.Meta):
        fields = CommandeSerializer.Meta.fields + ["articles", "adresse_livraison"]
        read_only_fields = fields

    @extend_schema_field(AdresseLivraisonAvecPositionLectureSerializer(allow_null=True))
    def get_adresse_livraison(self, commande):
        return adresse_du_groupe(commande.groupe, avec_position=True)


class ArticleVendeurSerializer(serializers.ModelSerializer):
    class Meta:
        model = CommandeItem
        fields = ["id", "variante", "nom_produit", "prix_unitaire", "quantite"]
        read_only_fields = fields


class CommandeVendeurSerializer(serializers.ModelSerializer):
    """Commande vue par le vendeur : le strict nécessaire pour la préparer
    et l'expédier. Jamais l'email ni le téléphone du profil du client (seul
    le téléphone de livraison, choisi par le client pour cette commande),
    jamais le point GPS du lieu de livraison."""

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


class ClientCommandeAdministrationSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    email = serializers.EmailField()
    nom = serializers.CharField()
    prenom = serializers.CharField()


class BoutiqueCommandeAdministrationSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    nom = serializers.CharField()
    slug = serializers.CharField()


class CommandeAdministrationSerializer(CommandeVendeurSerializer):
    """Commande vue par l'administration : celle du vendeur, plus le client
    (identité et email, pour le support) et la boutique. Jamais le point GPS
    du lieu de livraison."""

    client = ClientCommandeAdministrationSerializer(read_only=True)
    boutique = BoutiqueCommandeAdministrationSerializer(read_only=True)

    class Meta(CommandeVendeurSerializer.Meta):
        fields = CommandeVendeurSerializer.Meta.fields + ["boutique"]
        read_only_fields = fields


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
