import uuid
from decimal import Decimal
from django.db import models, transaction
from django.conf import settings
from django.utils import timezone

from .signals import paiement_valide


#: Clés de Paiement.metadata écrites par le backend uniquement.
CLES_METADATA_INTERNES = ("commandes_couvertes", "remboursements_dus")


class Paiement(models.Model):
    """Transaction de paiement liée à une commande unique ou un groupe de commandes multi-boutiques."""

    class Methode(models.TextChoices):
        WAVE = "wave", "Wave"
        ORANGE_MONEY = "orange_money", "Orange Money"
        MTN_MONEY = "mtn_money", "MTN Mobile Money"
        MOOV_MONEY = "moov_money", "Moov Money"
        CARTE_BANCAIRE = "carte_bancaire", "Carte Bancaire (Visa / Mastercard)"
        ESPECE_LIVRAISON = "espece_livraison", "Paiement à la livraison (Cash on Delivery)"

    class Statut(models.TextChoices):
        EN_ATTENTE = "en_attente", "En attente"
        VALIDE = "valide", "Validé"
        ECHOUE = "echoue", "Échoué"
        ANNULE = "annule", "Annulé"
        REMBOURSE = "rembourse", "Remboursé"
        # Encaissé, mais la commande est annulée (paiement reçu après
        # expiration, ou annulation d'une commande payée) : remboursement à
        # traiter par l'administration (détail dans metadata.remboursements_dus).
        A_REMBOURSER = "a_rembourser", "À rembourser"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=30, unique=True, editable=False, db_index=True)

    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="paiements",
        verbose_name="Client",
    )

    commande = models.ForeignKey(
        "commandes.Commande",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="paiements",
        verbose_name="Commande associée",
    )

    groupe_commande = models.ForeignKey(
        "commandes.GroupeCommande",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="paiements",
        verbose_name="Groupe de commandes associé",
    )

    methode = models.CharField(
        max_length=25,
        choices=Methode.choices,
        default=Methode.WAVE,
    )

    statut = models.CharField(
        max_length=20,
        choices=Statut.choices,
        default=Statut.EN_ATTENTE,
        db_index=True,
    )

    montant = models.DecimalField(max_digits=12, decimal_places=2)
    devise = models.CharField(max_length=3, default="XOF")

    transaction_id_externe = models.CharField(
        max_length=150,
        null=True,
        blank=True,
        db_index=True,
        help_text="Identifiant de transaction retourné par la passerelle de paiement",
    )

    url_paiement = models.URLField(
        max_length=500,
        null=True,
        blank=True,
        help_text="URL de paiement externe ou de redirection vers le checkout de la passerelle",
    )

    # Reprise de l'adresse saisie à la validation du panier
    # (GroupeCommande) : plus aucune valeur par défaut fictive.
    adresse_livraison = models.CharField(
        max_length=255,
        blank=True,
        help_text="Adresse de livraison à transmettre au module de livraison",
    )

    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Données brutes retournées par la passerelle ou informations contextuelles",
    )

    date_creation = models.DateTimeField(auto_now_add=True)
    date_validation = models.DateTimeField(null=True, blank=True)
    date_mise_a_jour = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Paiement"
        verbose_name_plural = "Paiements"
        ordering = ["-date_creation"]

    def save(self, *args, **kwargs):
        if not self.reference:
            annee = timezone.now().year
            self.reference = f"PAY-{annee}-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.reference} — {self.montant} {self.devise} ({self.get_statut_display()})"

    @property
    def est_regle(self):
        return self.statut == self.Statut.VALIDE

    def valider(self, transaction_id_externe=None, donnees_supplementaires=None):
        """Valide le paiement de manière atomique et déclenche le signal métier.

        Si une commande couverte a été annulée entre-temps (paiement reçu
        après l'expiration du délai, par exemple), la commande n'est PAS
        réactivée : le paiement passe « à rembourser », l'administration
        est alertée, et le signal métier n'est pas émis (ni confirmation,
        ni points de fidélité).
        """
        from apps.commandes.models import Commande
        from .services import commandes_couvertes, marquer_a_rembourser

        if self.statut in (self.Statut.VALIDE, self.Statut.A_REMBOURSER):
            # Déjà traité (protection contre réceptions multiples de webhooks)
            return

        with transaction.atomic():
            self.statut = self.Statut.VALIDE
            self.date_validation = timezone.now()

            if transaction_id_externe:
                self.transaction_id_externe = transaction_id_externe

            if donnees_supplementaires:
                # Les clés internes (commandes couvertes, remboursements dus)
                # ne viennent jamais de la passerelle.
                donnees = {
                    cle: valeur for cle, valeur in donnees_supplementaires.items()
                    if cle not in CLES_METADATA_INTERNES
                }
                self.metadata = {**self.metadata, **donnees}

            self.save(update_fields=[
                "statut",
                "date_validation",
                "transaction_id_externe",
                "metadata",
                "date_mise_a_jour",
            ])

            commandes_annulees = [
                commande for commande in commandes_couvertes(self)
                if commande.status == Commande.Status.ANNULEE
            ]
            if commandes_annulees:
                for commande in commandes_annulees:
                    marquer_a_rembourser(self, commande, "paiement reçu pour une commande annulée")
                return

            # Émission du signal pour notifier les modules commandes et livraison
            paiement_valide.send(
                sender=self.__class__,
                paiement=self,
                client=self.client,
                adresse_livraison=self.adresse_livraison,
            )

    def marquer_echoue(self, motif="", donnees_supplementaires=None):
        """Marque le paiement comme ayant échoué."""
        if self.statut in (self.Statut.VALIDE, self.Statut.A_REMBOURSER):
            return

        self.statut = self.Statut.ECHOUE
        meta = {**self.metadata}
        if motif:
            meta["motif_echec"] = motif
        if donnees_supplementaires:
            meta.update(donnees_supplementaires)
        self.metadata = meta
        self.save(update_fields=["statut", "metadata", "date_mise_a_jour"])

    def marquer_annule(self, motif=""):
        """Annule le paiement si non validé."""
        if self.statut in (self.Statut.VALIDE, self.Statut.A_REMBOURSER):
            return
        self.statut = self.Statut.ANNULE
        if motif:
            self.metadata = {**self.metadata, "motif_annulation": motif}
        self.save(update_fields=["statut", "metadata", "date_mise_a_jour"])


class JournalWebhook(models.Model):
    """Journal d'audit et d'idempotence des événements webhooks reçus des passerelles."""

    class StatutTraitement(models.TextChoices):
        TRAITE = "traite", "Traité"
        IGNORE = "ignore", "Ignoré (Doublon)"
        ERREUR = "erreur", "Erreur de traitement"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fournisseur = models.CharField(max_length=50, db_index=True)
    evenement_id = models.CharField(max_length=150, db_index=True)
    payload = models.JSONField(default=dict)
    statut_traitement = models.CharField(
        max_length=20,
        choices=StatutTraitement.choices,
        default=StatutTraitement.TRAITE,
    )
    erreur = models.TextField(blank=True)
    date_reception = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Journal Webhook"
        verbose_name_plural = "Journaux Webhooks"
        ordering = ["-date_reception"]
        constraints = [
            models.UniqueConstraint(
                fields=["fournisseur", "evenement_id"],
                name="unique_webhook_fournisseur_evenement"
            )
        ]

    def __str__(self):
        return f"Webhook {self.fournisseur}:{self.evenement_id} ({self.statut_traitement})"
