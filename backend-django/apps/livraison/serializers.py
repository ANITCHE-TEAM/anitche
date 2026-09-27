"""Représentations d'une livraison, une par rôle (strict nécessaire) :

- client  : suivi (prénom du livreur, son téléphone pendant « en cours »
            seulement, estimation, code de livraison pendant « en cours ») ;
- livreur : adresse et téléphone du client, masqués une fois la livraison
            terminée (livrée ou annulée) ;
- vendeur : statut, prénom du livreur, dates — jamais le code ;
- administration : tout sauf le code.

`livreur`, `status` et les dates ne s'écrivent jamais par un serializer :
uniquement par apps.livraison.services (table des transitions, historique).
"""

from datetime import datetime
from typing import Optional

from django.utils import timezone
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.commandes.serializers import AdresseLivraisonLectureSerializer, adresse_du_groupe

from .models import ContestationLivraison, Livraison, LivraisonHistorique, TarifLivraison, normaliser_commune
from .services import CODE_ESSAIS_MAX, STATUTS_TERMINES, fin_du_delai_de_contestation, role_acteur

CHAMPS_COMMUNS = [
    "id", "commande", "numero_commande", "livreur", "status",
    "date_livraison_estimee", "date_expedition", "date_livraison", "created_at", "updated_at",
]


# ---------------------------------------------------------------------
# Documentation OpenAPI des objets imbriqués construits à la main
# (livreur et contestation, différents selon le rôle).
# ---------------------------------------------------------------------

class LivreurVuParLaBoutiqueSerializer(serializers.Serializer):
    prenom = serializers.CharField()


class LivreurVuParLeClientSerializer(serializers.Serializer):
    prenom = serializers.CharField()
    telephone = serializers.CharField(allow_null=True, help_text="Seulement pendant « en cours ».")


class LivreurVuParLAdministrationSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    prenom = serializers.CharField()
    nom = serializers.CharField()


class ContestationVueParLeClientSerializer(serializers.Serializer):
    statut = serializers.ChoiceField(choices=ContestationLivraison.Statut.choices)
    date_creation = serializers.DateTimeField()
    date_resolution = serializers.DateTimeField(allow_null=True)


class ContestationVueParLAdministrationSerializer(ContestationVueParLeClientSerializer):
    id = serializers.UUIDField()
    motif = serializers.CharField()
    commentaire_resolution = serializers.CharField()


def _contestation(livraison):
    try:
        return livraison.contestation
    except ContestationLivraison.DoesNotExist:
        return None


class _LivraisonBaseSerializer(serializers.ModelSerializer):
    numero_commande = serializers.CharField(source="commande.numero_commande", read_only=True)
    livreur = serializers.SerializerMethodField()

    @extend_schema_field(LivreurVuParLaBoutiqueSerializer(allow_null=True))
    def get_livreur(self, livraison):
        if livraison.livreur is None:
            return None
        return {"prenom": livraison.livreur.prenom}


class _CoordonneesClientMixin(serializers.Serializer):
    """Adresse (texte et structurée) et téléphone choisi par le client au
    checkout (GroupeCommande) — jamais le téléphone de son profil."""

    adresse_livraison = serializers.CharField(read_only=True)
    adresse = serializers.SerializerMethodField()
    telephone_contact = serializers.SerializerMethodField()

    @extend_schema_field(AdresseLivraisonLectureSerializer(allow_null=True))
    def get_adresse(self, livraison):
        return adresse_du_groupe(livraison.commande.groupe)

    def get_telephone_contact(self, livraison) -> str:
        groupe = livraison.commande.groupe
        return groupe.livraison_telephone if groupe else ""


class LivraisonClientSerializer(_CoordonneesClientMixin, _LivraisonBaseSerializer):
    code_livraison = serializers.SerializerMethodField()
    contestation = serializers.SerializerMethodField()
    date_limite_contestation = serializers.SerializerMethodField()

    class Meta:
        model = Livraison
        fields = CHAMPS_COMMUNS + [
            "adresse_livraison", "adresse", "telephone_contact", "code_livraison",
            "contestation", "date_limite_contestation",
        ]
        read_only_fields = fields

    @extend_schema_field(LivreurVuParLeClientSerializer(allow_null=True))
    def get_livreur(self, livraison):
        if livraison.livreur is None:
            return None
        # Téléphone du livreur : seulement pendant la tournée.
        telephone = livraison.livreur.telephone if livraison.status == Livraison.Status.EN_COURS else None
        return {"prenom": livraison.livreur.prenom, "telephone": telephone}

    def get_code_livraison(self, livraison) -> Optional[str]:
        if livraison.status != Livraison.Status.EN_COURS:
            return None
        return livraison.code_chiffre or None

    @extend_schema_field(ContestationVueParLeClientSerializer(allow_null=True))
    def get_contestation(self, livraison):
        contestation = _contestation(livraison)
        if contestation is None:
            return None
        return {
            "statut": contestation.statut,
            "date_creation": contestation.date_creation,
            "date_resolution": contestation.date_resolution,
        }

    def get_date_limite_contestation(self, livraison) -> Optional[datetime]:
        if livraison.status != Livraison.Status.LIVREE or not livraison.date_livraison:
            return None
        return fin_du_delai_de_contestation(livraison)


class LivraisonLivreurSerializer(_CoordonneesClientMixin, _LivraisonBaseSerializer):
    code_essais_restants = serializers.SerializerMethodField()

    class Meta:
        model = Livraison
        fields = CHAMPS_COMMUNS + [
            "adresse_livraison", "adresse", "telephone_contact", "tentatives", "code_essais_restants",
        ]
        read_only_fields = fields

    def to_representation(self, livraison):
        donnees = super().to_representation(livraison)
        # Livraison terminée : le livreur n'a plus besoin des coordonnées du client.
        if livraison.status in STATUTS_TERMINES:
            donnees.update({"adresse_livraison": "", "adresse": None, "telephone_contact": ""})
        return donnees

    def get_code_essais_restants(self, livraison) -> Optional[int]:
        if livraison.status != Livraison.Status.EN_COURS:
            return None
        return max(CODE_ESSAIS_MAX - livraison.code_essais, 0)


class LivraisonVendeurSerializer(_LivraisonBaseSerializer):
    class Meta:
        model = Livraison
        fields = CHAMPS_COMMUNS
        read_only_fields = fields


class LivraisonAdministrationSerializer(_CoordonneesClientMixin, _LivraisonBaseSerializer):
    contestation = serializers.SerializerMethodField()

    class Meta:
        model = Livraison
        fields = CHAMPS_COMMUNS + [
            "adresse_livraison", "adresse", "telephone_contact", "tentatives", "code_essais", "contestation",
        ]
        read_only_fields = fields

    @extend_schema_field(LivreurVuParLAdministrationSerializer(allow_null=True))
    def get_livreur(self, livraison):
        if livraison.livreur is None:
            return None
        return {"id": livraison.livreur.pk, "prenom": livraison.livreur.prenom, "nom": livraison.livreur.nom}

    @extend_schema_field(ContestationVueParLAdministrationSerializer(allow_null=True))
    def get_contestation(self, livraison):
        contestation = _contestation(livraison)
        if contestation is None:
            return None
        return {
            "id": contestation.pk,
            "statut": contestation.statut,
            "motif": contestation.motif,
            "commentaire_resolution": contestation.commentaire_resolution,
            "date_creation": contestation.date_creation,
            "date_resolution": contestation.date_resolution,
        }


class LivraisonHistoriqueSerializer(serializers.ModelSerializer):
    """Le rôle de l'auteur remplace son identifiant."""

    acteur = serializers.SerializerMethodField()

    class Meta:
        model = LivraisonHistorique
        fields = ["id", "livraison", "ancien_status", "nouveau_status", "acteur", "commentaire", "created_at"]
        read_only_fields = fields

    def get_acteur(self, ligne) -> str:
        # Lignes antérieures au champ role_acteur : rôle actuel de l'auteur.
        return ligne.role_acteur or role_acteur(ligne.effectue_par)


class LivraisonHistoriqueAdministrationSerializer(LivraisonHistoriqueSerializer):
    class Meta(LivraisonHistoriqueSerializer.Meta):
        fields = LivraisonHistoriqueSerializer.Meta.fields + ["effectue_par"]
        read_only_fields = fields


# ---------- Payloads ----------

class LivraisonChangerStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=Livraison.Status.choices)
    commentaire = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")
    # Obligatoire pour « livrée » (vérifié par le service).
    code = serializers.CharField(required=False, allow_blank=True, max_length=10, default="")


class AssignerLivreurSerializer(serializers.Serializer):
    livreur_id = serializers.IntegerField()
    date_livraison_estimee = serializers.DateField(required=False)

    def validate_date_livraison_estimee(self, date):
        if date < timezone.localdate():
            raise serializers.ValidationError("La date estimée ne peut pas être dans le passé.")
        return date


class AbandonnerLivraisonSerializer(serializers.Serializer):
    commentaire = serializers.CharField(max_length=255)


class ContesterLivraisonSerializer(serializers.Serializer):
    motif = serializers.CharField(max_length=1000)


class ResoudreContestationSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=[
        (ContestationLivraison.Statut.REJETEE, "Rejetée"), (ContestationLivraison.Statut.FONDEE, "Fondée"),
    ])
    commentaire = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")


class ContestationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContestationLivraison
        fields = ["id", "livraison", "motif", "statut", "commentaire_resolution", "date_creation", "date_resolution"]
        read_only_fields = fields


class NommerLivreurSerializer(serializers.Serializer):
    utilisateur_id = serializers.IntegerField()


class LivreurSerializer(serializers.Serializer):
    """Livreur vu par l'administration (choix pour l'assignation)."""

    id = serializers.IntegerField()
    prenom = serializers.CharField()
    nom = serializers.CharField()
    email = serializers.EmailField()
    telephone = serializers.CharField(allow_null=True)
    livraisons_en_cours = serializers.IntegerField(default=0)


class LivraisonAReassignerSerializer(serializers.ModelSerializer):
    numero_commande = serializers.CharField(source="commande.numero_commande", read_only=True)

    class Meta:
        model = Livraison
        fields = ["id", "numero_commande", "status"]
        read_only_fields = fields



# =====================================================================
# TARIFS DE LIVRAISON
# =====================================================================

class TarifLivraisonSerializer(serializers.ModelSerializer):
    """Administration. La zone et la commune d'un tarif ne changent plus
    après sa création (on crée un autre tarif) ; le tarif par défaut d'une
    zone ne se désactive pas : sans lui, plus aucune commande possible.

    Créer un tarif de commune dans la zone « abidjan » ajoute la commune au
    district : les adresses de cette commune passent en zone Abidjan. Une
    commune désactivée reste dans sa zone et prend le tarif par défaut."""

    class Meta:
        model = TarifLivraison
        fields = ["id", "zone", "commune", "montant", "est_actif", "modifie_par", "date_creation", "date_mise_a_jour"]
        read_only_fields = ["id", "modifie_par", "date_creation", "date_mise_a_jour"]
        # Les validateurs générés depuis les contraintes conditionnelles
        # ignorent leur condition (« zone unique » tout court) : validate()
        # applique les vraies règles, la base les garantit.
        validators = []
        extra_kwargs = {"zone": {"validators": []}}

    def validate(self, attrs):
        instance = self.instance
        if instance is not None:
            for champ in ("zone", "commune"):
                if champ in attrs and attrs[champ].strip() != getattr(instance, champ):
                    raise serializers.ValidationError(
                        {champ: "Non modifiable : créez un nouveau tarif (et désactivez celui-ci si besoin)."}
                    )
        zone = attrs.get("zone", getattr(instance, "zone", None))
        commune_normalisee = normaliser_commune(attrs.get("commune", getattr(instance, "commune", "")))
        if attrs.get("est_actif") is False and not commune_normalisee:
            raise serializers.ValidationError(
                {"est_actif": "Le tarif par défaut d'une zone ne peut pas être désactivé : modifiez son montant."}
            )
        # Une commune n'a qu'un tarif, toutes zones confondues : sa zone
        # (déduite par le serveur) ne peut pas être ambiguë.
        doublon = TarifLivraison.objects.filter(commune_normalisee=commune_normalisee)
        if not commune_normalisee:
            doublon = doublon.filter(zone=zone)
        if instance is not None:
            doublon = doublon.exclude(pk=instance.pk)
        existant = doublon.first()
        if existant is not None:
            raise serializers.ValidationError({"commune": (
                f"Un tarif existe déjà pour cette commune (zone {existant.get_zone_display()})."
                if commune_normalisee else "Cette zone a déjà un tarif par défaut."
            )})
        return attrs


# ---------------------------------------------------------------------
# Documentation OpenAPI des réponses construites dans les vues.
# ---------------------------------------------------------------------

class TarifCommuneSerializer(serializers.Serializer):
    commune = serializers.CharField()
    zone = serializers.ChoiceField(choices=TarifLivraison.Zone.choices)
    montant = serializers.IntegerField(help_text="FCFA.")


class TarifAutresVillesSerializer(serializers.Serializer):
    zone = serializers.ChoiceField(choices=TarifLivraison.Zone.choices)
    montant = serializers.IntegerField(help_text="FCFA.")


class GrilleTarifsSerializer(serializers.Serializer):
    communes = TarifCommuneSerializer(many=True, help_text="Menu déroulant du checkout.")
    autres_villes = TarifAutresVillesSerializer(allow_null=True, help_text="Tarif des villes sans tarif propre.")


class LivreurRetireSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    prenom = serializers.CharField()
    nom = serializers.CharField()
    role = serializers.CharField()


class RetraitLivreurSerializer(serializers.Serializer):
    utilisateur = LivreurRetireSerializer()
    livraisons_a_reassigner = LivraisonAReassignerSerializer(many=True)
