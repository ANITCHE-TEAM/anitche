from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.core.adresses import (
    champ_latitude,
    champ_longitude,
    valider_position_complete,
    valider_telephone,
)

from .models import Commande, CommandeHistorique, GroupeCommande, CommandeItem

MESSAGE_ADRESSE_OBLIGATOIRE = "L'adresse de livraison est obligatoire."
MESSAGE_ADRESSE_EN_DOUBLE = "Envoyez soit `adresse_id` (carnet), soit `adresse_livraison` (saisie), pas les deux."


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

    `destinataire` et `indications` : facultatifs, pour le livreur (personne
    qui reçoit le colis, consignes d'accès).
    """

    commune = serializers.CharField(max_length=100)
    quartier = serializers.CharField(max_length=150)
    point_de_repere = serializers.CharField(max_length=500)
    telephone = serializers.CharField(max_length=20)
    destinataire = serializers.CharField(max_length=150, required=False, allow_blank=True)
    indications = serializers.CharField(max_length=500, required=False, allow_blank=True)
    latitude = champ_latitude()
    longitude = champ_longitude()

    def validate_telephone(self, telephone):
        return valider_telephone(telephone)

    def validate(self, attrs):
        return valider_position_complete(attrs)


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
    avec le point GPS (`null` quand le client ne l'a pas donné), le
    destinataire et les indications d'accès (vides quand le client n'en a
    pas donné)."""

    destinataire = serializers.CharField(allow_blank=True)
    indications = serializers.CharField(allow_blank=True)
    latitude = serializers.FloatField(allow_null=True)
    longitude = serializers.FloatField(allow_null=True)


def _degres(valeur):
    return None if valeur is None else float(valeur)


def adresse_du_groupe(groupe, avec_position=False):
    """Adresse du checkout, ou None pour un groupe sans adresse.

    `avec_position` ajoute le point GPS (nombres, ou null), le destinataire et
    les indications d'accès. Réservé au client de la commande, au livreur
    assigné (masqué avec toute l'adresse une fois la livraison terminée) et à
    l'administration : jamais au vendeur, pour qui l'adresse texte suffit à
    préparer le colis.
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
        adresse["destinataire"] = groupe.livraison_destinataire
        adresse["indications"] = groupe.livraison_indications
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


class CommandeHistoriqueSerializer(serializers.ModelSerializer):
    """Client et vendeur : l'origine du changement (`source`), jamais le compte
    qui l'a fait."""

    class Meta:
        model = CommandeHistorique
        fields = ["id", "ancien_statut", "nouveau_statut", "source", "motif", "created_at"]
        read_only_fields = fields


class CommandeHistoriqueAdministrationSerializer(CommandeHistoriqueSerializer):
    class Meta(CommandeHistoriqueSerializer.Meta):
        fields = CommandeHistoriqueSerializer.Meta.fields + ["auteur"]
        read_only_fields = fields


class SimulerFraisSerializer(serializers.Serializer):
    """Entrée de la simulation du checkout (mêmes champs que la validation).

    L'adresse est choisie dans le carnet du client (`adresse_id`) OU saisie
    (`adresse_livraison`) : l'une des deux, pas les deux."""

    adresse_id = serializers.UUIDField(
        required=False, help_text="Adresse du carnet du client. Exclusif avec `adresse_livraison`.",
    )
    adresse_livraison = AdresseLivraisonSerializer(
        required=False, help_text="Adresse saisie. Exclusif avec `adresse_id`.",
    )
    coupon_code = serializers.CharField(max_length=30, required=False, allow_blank=True)

    def validate(self, attrs):
        if "adresse_id" in attrs and "adresse_livraison" in attrs:
            raise serializers.ValidationError({"adresse_id": MESSAGE_ADRESSE_EN_DOUBLE})
        if "adresse_id" not in attrs and "adresse_livraison" not in attrs:
            raise serializers.ValidationError({"adresse_livraison": MESSAGE_ADRESSE_OBLIGATOIRE})
        return attrs


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
