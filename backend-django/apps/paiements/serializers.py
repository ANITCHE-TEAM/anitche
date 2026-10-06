from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from .fournisseurs.base import ECHEC, SUCCES
from .models import BaremeFrais, Paiement, Remboursement, Reversement
from .reversements import OPERATEURS

#: Codes machine (errors.code) des refus sur les barèmes. Un barème commencé
#: a pu s'appliquer à des ventes : il ne se modifie (sauf sa clôture) ni ne
#: se supprime plus. Un barème ne commence jamais dans le passé : il
#: deviendrait rétroactivement « en vigueur » sur une période déjà vendue.
CODE_BAREME_DEJA_APPLIQUE = "bareme_deja_applique"
CODE_DATE_DEBUT_PASSEE = "date_debut_passee"
#: « Maintenant » envoyé par un client dont l'horloge retarde un peu (ou
#: reçu avec retard), pour clôturer ou pour commencer : ramené à l'heure du
#: serveur, jamais avant.
TOLERANCE_MAINTENANT = timedelta(minutes=1)
NON_MODIFIABLE = "Non modifiable : le barème a déjà commencé."
MAINTENANT_OU_PLUS_TARD = "Maintenant ou plus tard."


def refus(code, message, champs):
    """Erreur au format commun : message dans detail, code machine dans
    errors.code, message de chaque champ en cause."""
    return {"detail": message, "code": code, **champs}


class InitierPaiementSerializer(serializers.Serializer):
    """Entrée seulement : le montant est toujours calculé par le serveur."""

    commande_id = serializers.UUIDField(required=False, allow_null=True)
    groupe_commande_id = serializers.UUIDField(required=False, allow_null=True)
    methode = serializers.CharField(max_length=25, default=Paiement.Methode.WAVE)
    # Dépréciés et ignorés (conservés pour ne pas casser un client qui les
    # enverrait encore) : le numéro est saisi sur la page du fournisseur,
    # l'adresse vient de la validation du panier.
    telephone = serializers.CharField(required=False, allow_blank=True, max_length=25)
    adresse_livraison = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate_methode(self, methode):
        if methode == "espece_livraison":
            raise serializers.ValidationError(
                "Le paiement à la livraison n'est pas proposé : le paiement se fait en ligne avant la livraison."
            )
        if methode not in Paiement.Methode.values:
            raise serializers.ValidationError(
                f"Moyen de paiement inconnu. Moyens acceptés : {', '.join(Paiement.Methode.values)}."
            )
        return methode

    def validate(self, attrs):
        if bool(attrs.get("commande_id")) == bool(attrs.get("groupe_commande_id")):
            raise serializers.ValidationError(
                "Spécifiez soit 'commande_id', soit 'groupe_commande_id' (un seul des deux)."
            )
        return attrs


class RemboursementClientSerializer(serializers.ModelSerializer):
    statut_display = serializers.CharField(source="get_statut_display", read_only=True)
    motif_display = serializers.CharField(source="get_motif_display", read_only=True)

    class Meta:
        model = Remboursement
        fields = ["reference", "commande", "montant", "motif", "motif_display", "statut", "statut_display",
                  "date_creation", "date_traitement"]
        read_only_fields = fields


class PaiementSerializer(serializers.ModelSerializer):
    """Vue client : aucune donnée interne ni brute du fournisseur."""

    client_email = serializers.EmailField(source="client.email", read_only=True)
    methode_display = serializers.CharField(source="get_methode_display", read_only=True)
    statut_display = serializers.CharField(source="get_statut_display", read_only=True)
    commandes = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    remboursements = RemboursementClientSerializer(many=True, read_only=True)

    class Meta:
        model = Paiement
        fields = [
            "id", "reference", "client", "client_email", "commande", "groupe_commande", "commandes",
            "methode", "methode_display", "statut", "statut_display", "montant", "devise",
            "url_paiement", "adresse_livraison", "remboursements",
            "date_creation", "date_validation", "date_mise_a_jour",
        ]
        read_only_fields = fields


class PaiementAdminSerializer(PaiementSerializer):
    """Vue administration : ajoute le fournisseur et ses identifiants."""

    class Meta(PaiementSerializer.Meta):
        fields = PaiementSerializer.Meta.fields + ["fournisseur", "transaction_id_externe", "metadata"]
        read_only_fields = fields


class RemboursementAdminSerializer(serializers.ModelSerializer):
    numero_commande = serializers.CharField(source="commande.numero_commande", read_only=True)
    reference_paiement = serializers.CharField(source="paiement.reference", read_only=True)
    client_email = serializers.EmailField(source="paiement.client.email", read_only=True)
    retour = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = Remboursement
        fields = ["id", "reference", "reference_paiement", "commande", "numero_commande", "client_email",
                  "retour", "montant", "motif", "statut", "reference_externe", "commentaire",
                  "traite_par", "date_creation", "date_traitement"]
        read_only_fields = fields


class SimulationPaiementSerializer(serializers.Serializer):
    """Issue choisie sur la page de paiement simulé (développement)."""

    statut = serializers.ChoiceField(choices=[SUCCES, ECHEC])


class TraiterRemboursementSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=[Remboursement.Statut.EFFECTUE, Remboursement.Statut.REFUSE])
    reference_externe = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")
    commentaire = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if attrs["decision"] == Remboursement.Statut.EFFECTUE and not attrs["reference_externe"].strip():
            raise serializers.ValidationError(
                {"reference_externe": "Obligatoire : référence du remboursement chez le fournisseur."}
            )
        if attrs["decision"] == Remboursement.Statut.REFUSE and not attrs["commentaire"].strip():
            raise serializers.ValidationError({"commentaire": "Obligatoire : motif du refus."})
        return attrs


class LigneReversementSerializer(serializers.Serializer):
    """Article vendu et frais figés au moment de l'achat."""

    nom_produit = serializers.CharField()
    quantite = serializers.IntegerField()
    prix_unitaire = serializers.DecimalField(max_digits=12, decimal_places=2)
    taux_commission = serializers.DecimalField(max_digits=5, decimal_places=2)
    frais_fixe_unitaire = serializers.IntegerField()
    montant_commission = serializers.DecimalField(max_digits=12, decimal_places=2)
    montant_frais_fixes = serializers.DecimalField(max_digits=12, decimal_places=2)
    montant_net_vendeur = serializers.DecimalField(max_digits=12, decimal_places=2)


class ReversementVendeurSerializer(serializers.ModelSerializer):
    numero_commande = serializers.CharField(source="commande.numero_commande", read_only=True)
    statut_display = serializers.CharField(source="get_statut_display", read_only=True)
    lignes = LigneReversementSerializer(source="commande.article", many=True, read_only=True)

    class Meta:
        model = Reversement
        fields = [
            "id", "reference", "commande", "numero_commande", "statut", "statut_display",
            "montant_brut", "montant_commission", "montant_frais_fixes", "montant_retours", "montant_livraison",
            "montant_net",
            "montant_ajustements", "montant_a_verser", "lignes",
            "date_livraison", "date_disponibilite", "date_versement", "canal", "reference_externe",
        ]
        read_only_fields = fields


class ReversementAdminSerializer(ReversementVendeurSerializer):
    boutique_nom = serializers.CharField(source="boutique.nom", read_only=True)

    class Meta(ReversementVendeurSerializer.Meta):
        fields = ReversementVendeurSerializer.Meta.fields + ["boutique", "boutique_nom", "operateur",
                                                             "fournisseur", "verse_par"]
        read_only_fields = fields


class VerserReversementSerializer(serializers.Serializer):
    reference_externe = serializers.CharField(max_length=150, required=False, allow_blank=True, default="")


class TransfererReversementSerializer(serializers.Serializer):
    operateur = serializers.ChoiceField(choices=OPERATEURS, required=False)


class BaremeFraisSerializer(serializers.ModelSerializer):
    """Barème pas encore commencé : tout se modifie, date_debut maintenant ou
    plus tard. Barème commencé (date_debut passée) : seule sa clôture est
    possible (PUT et PATCH)."""

    class Meta:
        model = BaremeFrais
        fields = ["id", "boutique", "libelle", "taux_commission", "frais_fixe_article",
                  "seuil_petit_article", "frais_fixe_petit_article",
                  "date_debut", "date_fin", "cree_par", "date_creation"]
        read_only_fields = ["id", "cree_par", "date_creation"]

    def valider_cloture_seule(self, attrs, maintenant):
        """Barème commencé : aucun autre champ ne change (une valeur
        identique n'est pas une modification) ; date_fin ne change que pour
        clôturer, maintenant ou plus tard, un barème pas déjà clôturé."""
        bareme = self.instance
        modifies = [champ for champ, valeur in attrs.items() if champ != "date_fin" and getattr(bareme, champ) != valeur]
        if modifies:
            raise serializers.ValidationError(refus(
                CODE_BAREME_DEJA_APPLIQUE,
                "Barème déjà appliqué : il ne se modifie plus. Clôturez-le (date_fin) et créez-en un nouveau.",
                {champ: NON_MODIFIABLE for champ in modifies},
            ))
        if "date_fin" not in attrs or attrs["date_fin"] == bareme.date_fin:
            return attrs
        if bareme.date_fin is not None and bareme.date_fin <= maintenant:
            raise serializers.ValidationError(refus(
                CODE_BAREME_DEJA_APPLIQUE, "Barème déjà clôturé : sa date de fin ne change plus.",
                {"date_fin": NON_MODIFIABLE},
            ))
        fin = attrs["date_fin"]
        if fin is None or fin < maintenant - TOLERANCE_MAINTENANT:
            raise serializers.ValidationError(refus(
                CODE_BAREME_DEJA_APPLIQUE,
                "Un barème déjà appliqué ne peut être que clôturé : date_fin maintenant ou plus tard.",
                {"date_fin": MAINTENANT_OU_PLUS_TARD},
            ))
        return {**attrs, "date_fin": max(fin, maintenant)}

    def valider_date_debut(self, attrs, maintenant):
        """Création ou barème programmé : date_debut maintenant ou plus tard.
        Absente : inchangée (à la création, l'instant de l'enregistrement)."""
        debut = attrs.get("date_debut")
        if debut is None:
            return attrs
        if debut < maintenant - TOLERANCE_MAINTENANT:
            raise serializers.ValidationError(refus(
                CODE_DATE_DEBUT_PASSEE,
                "date_debut est dans le passé : un barème commence maintenant ou plus tard.",
                {"date_debut": MAINTENANT_OU_PLUS_TARD},
            ))
        return {**attrs, "date_debut": max(debut, maintenant)}

    def validate(self, attrs):
        maintenant = timezone.now()
        if self.instance is not None and self.instance.a_commence(maintenant):
            attrs = self.valider_cloture_seule(attrs, maintenant)
        else:
            attrs = self.valider_date_debut(attrs, maintenant)
        # Création sans date_debut : le modèle prendra l'instant de
        # l'enregistrement ; la comparaison avec date_fin se fait donc à
        # maintenant (sinon la contrainte en base répondrait 500).
        debut = attrs.get("date_debut", getattr(self.instance, "date_debut", maintenant))
        fin = attrs.get("date_fin", getattr(self.instance, "date_fin", None))
        if debut and fin and fin <= debut:
            raise serializers.ValidationError({"date_fin": "Doit être postérieure à date_debut."})
        seuil = attrs.get("seuil_petit_article", getattr(self.instance, "seuil_petit_article", None))
        reduit = attrs.get("frais_fixe_petit_article", getattr(self.instance, "frais_fixe_petit_article", None))
        if (seuil is None) != (reduit is None):
            champ = "frais_fixe_petit_article" if reduit is None else "seuil_petit_article"
            raise serializers.ValidationError(
                {champ: "seuil_petit_article et frais_fixe_petit_article vont ensemble (tous deux vides ou renseignés)."}
            )
        return attrs


class ResumeReversementsSerializer(serializers.Serializer):
    """Réponse de vendeur/reversements/resume/ (documentation OpenAPI) :
    montants nets en FCFA par étape, et nombre de reversements."""

    en_attente_livraison = serializers.IntegerField()
    nombre_en_attente_livraison = serializers.IntegerField()
    en_retractation = serializers.IntegerField(help_text="Suspendus (retour en cours) compris.")
    nombre_en_retractation = serializers.IntegerField()
    disponible = serializers.IntegerField(help_text="Versements en cours compris.")
    nombre_disponible = serializers.IntegerField()
    verse = serializers.IntegerField()
    nombre_verse = serializers.IntegerField()
    ajustements_en_attente = serializers.IntegerField()
    delai_retractation_jours = serializers.IntegerField()
