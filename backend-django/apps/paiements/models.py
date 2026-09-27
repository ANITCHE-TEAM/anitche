import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.core.fields import EncryptedCharField


def generer_reference(prefixe):
    """Référence lisible, unique, de moins de 30 caractères (limite CinetPay)."""
    return f"{prefixe}-{timezone.now().year}-{uuid.uuid4().hex[:10].upper()}"


class Paiement(models.Model):
    """Transaction d'encaissement d'une ou plusieurs commandes d'un même checkout.

    Le statut ne change que par apps.paiements.services (transitions
    contrôlées, sous verrou). Les commandes payées sont la relation
    `commandes` ; les remboursements dus sont des objets Remboursement.
    """

    class Methode(models.TextChoices):
        WAVE = "wave", "Wave"
        ORANGE_MONEY = "orange_money", "Orange Money"
        MTN_MONEY = "mtn_money", "MTN Mobile Money"
        MOOV_MONEY = "moov_money", "Moov Money"
        CARTE_BANCAIRE = "carte_bancaire", "Carte Bancaire (Visa / Mastercard)"
        # Le paiement à la livraison n'existe plus : les anciens paiements
        # « espece_livraison » gardent leur valeur (historique) mais ne
        # peuvent plus être créés (migration 0003 : passés annulés).

    class Statut(models.TextChoices):
        EN_ATTENTE = "en_attente", "En attente"
        VALIDE = "valide", "Validé"
        ECHOUE = "echoue", "Échoué"
        ANNULE = "annule", "Annulé"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=30, unique=True, editable=False)

    # PROTECT : un paiement est un historique financier, jamais effacé en
    # cascade avec un compte.
    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="paiements",
        verbose_name="Client",
    )

    # Commandes réellement payées par cette transaction.
    commandes = models.ManyToManyField(
        "commandes.Commande",
        related_name="paiements_couvrants",
        blank=True,
    )
    # Cible demandée par le client (contrat API conservé) : une commande
    # seule, ou le groupe (checkout) entier.
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

    # Moyen choisi par le client, distinct du fournisseur qui encaisse.
    methode = models.CharField(max_length=25, choices=Methode.choices, default=Methode.WAVE)
    fournisseur = models.CharField(max_length=30)

    statut = models.CharField(
        max_length=20,
        choices=Statut.choices,
        default=Statut.EN_ATTENTE,
        db_index=True,
    )

    # FCFA entiers (décimales toujours nulles).
    montant = models.DecimalField(max_digits=12, decimal_places=2)
    devise = models.CharField(max_length=3, default="XOF")

    transaction_id_externe = models.CharField(max_length=150, null=True, blank=True, db_index=True)
    # Empreinte SHA-256 du jeton de notification remis par le fournisseur à
    # l'initiation (jamais le jeton lui-même).
    hash_jeton_notification = models.CharField(max_length=64, blank=True)

    url_paiement = models.URLField(max_length=500, null=True, blank=True)

    # Reprise de l'adresse saisie à la validation du panier (GroupeCommande).
    adresse_livraison = models.CharField(max_length=255, blank=True)

    # Informations internes (motifs d'échec ou d'annulation), jamais
    # exposées au client ni alimentées par les notifications.
    metadata = models.JSONField(default=dict, blank=True)

    date_creation = models.DateTimeField(auto_now_add=True)
    date_validation = models.DateTimeField(null=True, blank=True)
    date_mise_a_jour = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Paiement"
        verbose_name_plural = "Paiements"
        ordering = ["-date_creation"]

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = generer_reference("PAY")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.reference} — {self.montant} {self.devise} ({self.get_statut_display()})"


class JournalWebhook(models.Model):
    """Journal d'audit et d'idempotence des notifications des fournisseurs."""

    class StatutTraitement(models.TextChoices):
        RECU = "recu", "Reçu"
        TRAITE = "traite", "Traité"
        IGNORE = "ignore", "Ignoré (transaction non finalisée)"
        ERREUR = "erreur", "Erreur de traitement"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    fournisseur = models.CharField(max_length=50, db_index=True)
    evenement_id = models.CharField(max_length=150, db_index=True)
    payload = models.JSONField(default=dict)
    statut_traitement = models.CharField(
        max_length=20,
        choices=StatutTraitement.choices,
        default=StatutTraitement.RECU,
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


class Remboursement(models.Model):
    """Somme encaissée à rendre au client, pour une commande.

    Traitement manuel par l'administration au lancement : elle rembourse
    depuis le tableau de bord du fournisseur puis saisit la référence.
    """

    class Motif(models.TextChoices):
        COMMANDE_ANNULEE = "commande_annulee", "Commande annulée après paiement"
        PAIEMENT_EN_DOUBLE = "paiement_en_double", "Commande déjà payée par un autre paiement"
        RETOUR = "retour", "Retour remboursé"
        LIVRAISON_NON_RECUE = "livraison_non_recue", "Livraison contestée (colis non reçu)"

    class Statut(models.TextChoices):
        A_TRAITER = "a_traiter", "À traiter"
        EFFECTUE = "effectue", "Effectué"
        REFUSE = "refuse", "Refusé"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=30, unique=True, editable=False)
    paiement = models.ForeignKey(Paiement, on_delete=models.PROTECT, related_name="remboursements")
    commande = models.ForeignKey("commandes.Commande", on_delete=models.PROTECT, related_name="remboursements")
    retour = models.OneToOneField(
        "retours.DemandeRetour",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="remboursement",
    )
    montant = models.DecimalField(max_digits=12, decimal_places=2)
    motif = models.CharField(max_length=30, choices=Motif.choices)
    statut = models.CharField(max_length=20, choices=Statut.choices, default=Statut.A_TRAITER, db_index=True)
    reference_externe = models.CharField(max_length=150, blank=True)
    commentaire = models.TextField(blank=True)
    traite_par = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    date_creation = models.DateTimeField(auto_now_add=True)
    date_traitement = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-date_creation"]
        constraints = [
            # Un seul remboursement par (paiement, commande) hors retours : une
            # annulation ou un doublon signalés deux fois ne créent rien de plus.
            models.UniqueConstraint(
                fields=["paiement", "commande"],
                condition=Q(retour__isnull=True),
                name="unique_remboursement_paiement_commande",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = generer_reference("RMB")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.reference} — {self.montant} FCFA ({self.get_statut_display()})"


class BaremeFrais(models.Model):
    """Frais vendeur (modèle Jumia) : commission en % + frais fixe par article.

    Barème de la plateforme (boutique vide) ou propre à une boutique (offre
    de lancement), valable entre date_debut et date_fin. Appliqué et figé
    dans chaque CommandeItem à la validation du panier : le modifier ne
    change jamais une vente passée.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    boutique = models.ForeignKey(
        "vendeurs.Boutique", on_delete=models.PROTECT, null=True, blank=True, related_name="baremes_frais",
    )
    libelle = models.CharField(max_length=100, blank=True)
    taux_commission = models.DecimalField(
        max_digits=5, decimal_places=2,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Commission en % du prix de vente (avant remise).",
    )
    frais_fixe_article = models.PositiveIntegerField(help_text="FCFA par article vendu.")
    date_debut = models.DateTimeField(default=timezone.now)
    date_fin = models.DateTimeField(null=True, blank=True)
    cree_par = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Barème de frais"
        verbose_name_plural = "Barèmes de frais"
        ordering = ["-date_debut"]
        constraints = [
            models.CheckConstraint(
                condition=Q(date_fin__isnull=True) | Q(date_fin__gt=models.F("date_debut")),
                name="bareme_frais_dates_coherentes",
            ),
            models.CheckConstraint(
                condition=Q(taux_commission__gte=0) & Q(taux_commission__lte=100),
                name="bareme_frais_taux_entre_0_et_100",
            ),
        ]

    def __str__(self):
        cible = self.boutique.nom if self.boutique_id else "plateforme"
        return f"{self.taux_commission} % + {self.frais_fixe_article} FCFA/article ({cible})"


class Reversement(models.Model):
    """Somme due au vendeur pour une commande, versée après livraison
    confirmée et délai de rétractation (REVERSEMENT_DELAI_RETRACTATION_JOURS).

    montant_net = brut − commission − frais fixes − retours − livraison
    offerte ; le montant réellement versé ajoute les ajustements négatifs
    imputés (retours survenus après un versement précédent, reste d'une
    livraison offerte, frais de livraison remboursés pour un retour
    imputable au vendeur).
    """

    class Statut(models.TextChoices):
        EN_ATTENTE_LIVRAISON = "en_attente_livraison", "En attente de livraison"
        EN_RETRACTATION = "en_retractation", "Délai de rétractation en cours"
        SUSPENDU = "suspendu", "Suspendu (retour en cours)"
        DISPONIBLE = "disponible", "Disponible"
        EN_COURS = "en_cours", "Versement en cours"
        VERSE = "verse", "Versé"
        ANNULE = "annule", "Annulé"

    class Canal(models.TextChoices):
        MOBILE_MONEY = "mobile_money", "Mobile money"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference = models.CharField(max_length=30, unique=True, editable=False)
    commande = models.OneToOneField("commandes.Commande", on_delete=models.PROTECT, related_name="reversement")
    boutique = models.ForeignKey("vendeurs.Boutique", on_delete=models.PROTECT, related_name="reversements")

    montant_brut = models.DecimalField(max_digits=12, decimal_places=2)
    montant_commission = models.DecimalField(max_digits=12, decimal_places=2)
    montant_frais_fixes = models.DecimalField(max_digits=12, decimal_places=2)
    # Part retirée par les retours remboursés avant le versement
    # (prix des articles retournés moins leur commission, rendue au vendeur ;
    # le frais fixe reste acquis à ANITCHE).
    montant_retours = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Livraison offerte par la boutique : son tarif (Commande.
    # frais_livraison_vendeur) est déduit ici, plafonné au net ; le reste
    # devient un AjustementVendeur à la livraison (apps.paiements.reversements).
    montant_livraison = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    montant_net = models.DecimalField(max_digits=12, decimal_places=2)
    # Ajustements négatifs imputés (≤ 0) et montant effectivement versé.
    montant_ajustements = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    montant_a_verser = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    statut = models.CharField(
        max_length=25, choices=Statut.choices, default=Statut.EN_ATTENTE_LIVRAISON, db_index=True,
    )
    date_livraison = models.DateTimeField(null=True, blank=True)
    date_disponibilite = models.DateTimeField(null=True, blank=True, db_index=True)
    date_versement = models.DateTimeField(null=True, blank=True)

    canal = models.CharField(max_length=20, choices=Canal.choices, default=Canal.MOBILE_MONEY)
    # Numéro du KYC figé au moment du versement (chiffré au repos).
    numero_destinataire = EncryptedCharField(blank=True, default="")
    operateur = models.CharField(max_length=20, blank=True)
    # Fournisseur du transfert (vide pour un versement manuel).
    fournisseur = models.CharField(max_length=30, blank=True)
    reference_externe = models.CharField(max_length=150, blank=True)
    hash_jeton_notification = models.CharField(max_length=64, blank=True)
    verse_par = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )

    date_creation = models.DateTimeField(auto_now_add=True)
    date_mise_a_jour = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date_creation"]
        indexes = [models.Index(fields=["boutique", "statut"])]

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = generer_reference("REV")
        super().save(*args, **kwargs)

    def recalculer_net(self):
        self.montant_net = max(
            self.montant_brut - self.montant_commission - self.montant_frais_fixes - self.montant_retours
            - self.montant_livraison,
            0,
        )

    def __str__(self):
        return f"{self.reference} — {self.montant_net} FCFA ({self.get_statut_display()})"


class AjustementVendeur(models.Model):
    """Montant négatif dû par une boutique, déduit du prochain reversement."""

    class Nature(models.TextChoices):
        # Retour remboursé après le versement de la commande (lié au retour).
        RETOUR = "retour", "Retour remboursé après versement"
        # Livraison offerte dont le tarif dépasse le net de la commande.
        LIVRAISON_OFFERTE = "livraison_offerte", "Livraison offerte (reste non couvert)"
        # Frais de livraison rendus au client pour un retour imputable au
        # vendeur (article manquant, défectueux, non conforme).
        FRAIS_LIVRAISON_RETOUR = "frais_livraison_retour", "Frais de livraison remboursés (retour)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    boutique = models.ForeignKey("vendeurs.Boutique", on_delete=models.PROTECT, related_name="ajustements")
    montant = models.DecimalField(max_digits=12, decimal_places=2, help_text="Négatif, en FCFA.")
    motif = models.CharField(max_length=255)
    nature = models.CharField(max_length=30, choices=Nature.choices, default=Nature.RETOUR)
    commande = models.ForeignKey(
        "commandes.Commande", on_delete=models.PROTECT, null=True, blank=True, related_name="ajustements_vendeur",
    )
    retour = models.OneToOneField(
        "retours.DemandeRetour", on_delete=models.PROTECT, null=True, blank=True, related_name="ajustement_vendeur",
    )
    reversement_impute = models.ForeignKey(
        Reversement, on_delete=models.SET_NULL, null=True, blank=True, related_name="ajustements_imputes",
    )
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date_creation"]
        constraints = [
            models.CheckConstraint(condition=Q(montant__lt=0), name="ajustement_vendeur_negatif"),
            # Livraison offerte et frais de livraison d'un retour : au plus
            # un ajustement de chaque nature par commande.
            models.UniqueConstraint(
                fields=["commande", "nature"],
                condition=~Q(nature="retour"),
                name="ajustement_vendeur_unique_par_commande",
            ),
        ]

    def __str__(self):
        return f"{self.montant} FCFA — {self.motif}"
