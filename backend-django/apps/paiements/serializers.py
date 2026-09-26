from decimal import Decimal
from rest_framework import serializers
from rest_framework.exceptions import ValidationError

from .models import Paiement, JournalWebhook
from apps.commandes.models import Commande, GroupeCommande


class InitierPaiementSerializer(serializers.Serializer):
    commande_id = serializers.UUIDField(required=False, allow_null=True)
    groupe_commande_id = serializers.UUIDField(required=False, allow_null=True)
    methode = serializers.ChoiceField(
        choices=Paiement.Methode.choices,
        default=Paiement.Methode.WAVE,
    )
    telephone = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=25,
        help_text="Numéro de téléphone pour Mobile Money (Wave, Orange, MTN, Moov)",
    )
    # Déprécié : ignoré. L'adresse est saisie à la validation du panier
    # (GroupeCommande) et reprise ici ; conservé pour ne pas casser un client
    # qui l'enverrait encore.
    adresse_livraison = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
        help_text="Déprécié et ignoré : l'adresse vient de la validation du panier.",
    )

    MESSAGE_INDISPONIBLE = (
        "Cette commande n'est plus disponible : elle a été annulée et son stock libéré."
    )
    MESSAGE_ADRESSE_MANQUANTE = (
        "Adresse de livraison manquante : elle se renseigne à la validation du panier."
    )

    def _annuler_si_boutique_indisponible(self, commandes):
        """Commande non payée d'une boutique qui n'est plus publiable
        (suspendue, fermée, vendeur non validé) : paiement refusé, commande
        annulée, stock restitué. Message volontairement générique."""
        from apps.commandes.services import TransitionImpossible, annuler_commande

        indisponibles = [c for c in commandes if not c.boutique.est_publiable]
        for commande in indisponibles:
            try:
                annuler_commande(commande, Commande.MotifAnnulation.BOUTIQUE_INDISPONIBLE)
            except TransitionImpossible:
                pass
        if indisponibles:
            raise ValidationError(self.MESSAGE_INDISPONIBLE)

    def _adresse(self, groupe):
        if groupe is None or not groupe.a_une_adresse:
            raise ValidationError(self.MESSAGE_ADRESSE_MANQUANTE)
        return groupe.adresse_livraison_texte

    def validate(self, attrs):
        commande_id = attrs.get("commande_id")
        groupe_id = attrs.get("groupe_commande_id")

        if not commande_id and not groupe_id:
            raise ValidationError("Vous devez spécifier soit 'commande_id' soit 'groupe_commande_id'.")

        if commande_id and groupe_id:
            raise ValidationError("Veuillez spécifier soit une commande unique, soit un groupe de commandes, pas les deux.")

        user = self.context["request"].user

        if commande_id:
            try:
                commande = Commande.objects.get(id=commande_id, client=user)
            except Commande.DoesNotExist:
                raise ValidationError({"commande_id": "Commande introuvable ou non autorisée."})

            if commande.status in (Commande.Status.CONFIRMEE, Commande.Status.PREPARATION, Commande.Status.EXPEDIEE, Commande.Status.LIVREE):
                raise ValidationError("Cette commande a déjà été payée ou confirmée.")

            if commande.status == Commande.Status.ANNULEE:
                raise ValidationError("Impossible de payer une commande annulée.")

            self._annuler_si_boutique_indisponible([commande])
            attrs["_adresse_livraison"] = self._adresse(commande.groupe)
            attrs["_commandes"] = [commande]

            # A04:2025 (Unrestricted Resource Consumption) + risque de
            # double-débit : sans ce contrôle, rien n'empêche un client
            # d'initier un nombre illimité de paiements EN_ATTENTE pour la
            # même commande (double-soumission frontend, ou usage
            # délibéré) — si plusieurs de ces sessions de paiement
            # distinctes aboutissent réellement chez la passerelle, le
            # client est débité plusieurs fois pour une seule commande.
            if Paiement.objects.filter(
                commande=commande, statut=Paiement.Statut.EN_ATTENTE
            ).exists():
                raise ValidationError(
                    "Un paiement est déjà en attente pour cette commande."
                )

            attrs["_cible_objet"] = commande
            attrs["_type_cible"] = "commande"
            attrs["_montant"] = commande.montant_total

        if groupe_id:
            try:
                groupe = GroupeCommande.objects.get(id=groupe_id, client=user)
            except GroupeCommande.DoesNotExist:
                raise ValidationError({"groupe_commande_id": "Groupe de commandes introuvable ou non autorisé."})

            # Les commandes déjà annulées (client, expiration, boutique
            # indisponible) sortent du groupe à payer.
            commandes = list(
                groupe.commandes.exclude(status=Commande.Status.ANNULEE).select_related("boutique__proprietaire")
            )
            if not commandes:
                raise ValidationError("Ce groupe de commandes ne contient aucune commande à payer.")

            deja_payee = any(c.status != Commande.Status.CREEE for c in commandes)
            if deja_payee:
                raise ValidationError("Une ou plusieurs commandes de ce groupe ont déjà été validées ou payées.")

            if Paiement.objects.filter(
                groupe_commande=groupe, statut=Paiement.Statut.EN_ATTENTE
            ).exists():
                raise ValidationError(
                    "Un paiement est déjà en attente pour ce groupe de commandes."
                )

            self._annuler_si_boutique_indisponible(commandes)
            attrs["_adresse_livraison"] = self._adresse(groupe)
            attrs["_commandes"] = commandes

            montant_total = sum((c.montant_total for c in commandes), Decimal("0.00"))
            attrs["_cible_objet"] = groupe
            attrs["_type_cible"] = "groupe"
            attrs["_montant"] = montant_total

        return attrs


class PaiementSerializer(serializers.ModelSerializer):
    client_email = serializers.EmailField(source="client.email", read_only=True)
    methode_display = serializers.CharField(source="get_methode_display", read_only=True)
    statut_display = serializers.CharField(source="get_statut_display", read_only=True)

    class Meta:
        model = Paiement
        fields = [
            "id",
            "reference",
            "client",
            "client_email",
            "commande",
            "groupe_commande",
            "methode",
            "methode_display",
            "statut",
            "statut_display",
            "montant",
            "devise",
            "transaction_id_externe",
            "url_paiement",
            "adresse_livraison",
            "metadata",
            "date_creation",
            "date_validation",
            "date_mise_a_jour",
        ]
        read_only_fields = fields


class WebhookPaiementSerializer(serializers.Serializer):
    fournisseur = serializers.CharField(max_length=50)
    evenement_id = serializers.CharField(max_length=150)
    reference = serializers.CharField(max_length=50)
    statut = serializers.ChoiceField(choices=["succes", "echec", "annule"])
    transaction_id_externe = serializers.CharField(max_length=150, required=False, allow_blank=True)
    metadata = serializers.DictField(required=False, default=dict)

    # Montant réellement débité côté passerelle. Requis sur un événement de
    # succès : sans lui, on validerait un Paiement au montant attendu sans
    # jamais vérifier que la passerelle a bien encaissé ce montant précis
    # (A08:2025 — Software/Data Integrity Failures).
    montant = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True,
    )

    def validate(self, attrs):
        if attrs.get("statut") == "succes" and attrs.get("montant") is None:
            raise serializers.ValidationError(
                {"montant": "Requis pour valider un paiement réussi."}
            )
        return attrs