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

from django.utils import timezone
from rest_framework import serializers

from apps.commandes.serializers import adresse_du_groupe

from .models import ContestationLivraison, Livraison, LivraisonHistorique
from .services import CODE_ESSAIS_MAX, STATUTS_TERMINES, fin_du_delai_de_contestation, role_acteur

CHAMPS_COMMUNS = [
    "id", "commande", "numero_commande", "livreur", "status",
    "date_livraison_estimee", "date_expedition", "date_livraison", "created_at", "updated_at",
]


def _contestation(livraison):
    try:
        return livraison.contestation
    except ContestationLivraison.DoesNotExist:
        return None


class _LivraisonBaseSerializer(serializers.ModelSerializer):
    numero_commande = serializers.CharField(source="commande.numero_commande", read_only=True)
    livreur = serializers.SerializerMethodField()

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

    def get_adresse(self, livraison):
        return adresse_du_groupe(livraison.commande.groupe)

    def get_telephone_contact(self, livraison):
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

    def get_livreur(self, livraison):
        if livraison.livreur is None:
            return None
        # Téléphone du livreur : seulement pendant la tournée.
        telephone = livraison.livreur.telephone if livraison.status == Livraison.Status.EN_COURS else None
        return {"prenom": livraison.livreur.prenom, "telephone": telephone}

    def get_code_livraison(self, livraison):
        if livraison.status != Livraison.Status.EN_COURS:
            return None
        return livraison.code_chiffre or None

    def get_contestation(self, livraison):
        contestation = _contestation(livraison)
        if contestation is None:
            return None
        return {
            "statut": contestation.statut,
            "date_creation": contestation.date_creation,
            "date_resolution": contestation.date_resolution,
        }

    def get_date_limite_contestation(self, livraison):
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

    def get_code_essais_restants(self, livraison):
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

    def get_livreur(self, livraison):
        if livraison.livreur is None:
            return None
        return {"id": livraison.livreur.pk, "prenom": livraison.livreur.prenom, "nom": livraison.livreur.nom}

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

    def get_acteur(self, ligne):
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

