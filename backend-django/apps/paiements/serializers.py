from rest_framework import serializers

from .models import BaremeFrais, Paiement, Remboursement, Reversement
from .reversements import OPERATEURS


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
    class Meta:
        model = BaremeFrais
        fields = ["id", "boutique", "libelle", "taux_commission", "frais_fixe_article",
                  "date_debut", "date_fin", "cree_par", "date_creation"]
        read_only_fields = ["id", "cree_par", "date_creation"]

    def validate(self, attrs):
        debut = attrs.get("date_debut", getattr(self.instance, "date_debut", None))
        fin = attrs.get("date_fin", getattr(self.instance, "date_fin", None))
        if debut and fin and fin <= debut:
            raise serializers.ValidationError({"date_fin": "Doit être postérieure à date_debut."})
        return attrs
