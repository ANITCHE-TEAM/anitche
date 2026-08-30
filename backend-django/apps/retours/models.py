import uuid
from decimal import Decimal
from django.db import models, transaction
from django.conf import settings
from django.utils import timezone


class DemandeRetour(models.Model):
    """Demande de retour et de remboursement / échange initiée par un client suite à une livraison."""

    class Motif(models.TextChoices):
        PRODUIT_DEFECTUEUX = "produit_defectueux", "Produit défectueux ou endommagé"
        NON_CONFORME = "non_conforme", "Article non conforme à la description"
        MAUVAISE_TAILLE = "mauvaise_taille", "Taille ou variante incorrecte"
        ARTICLE_MANQUANT = "article_manquant", "Article manquant dans le colis"
        CHANGEMENT_AVIS = "changement_avis", "Changement d'avis (droit de rétractation)"
        AUTRE = "autre", "Autre motif"

    class Statut(models.TextChoices):
        DEMANDE = "demande", "Demande soumise"
        APPROUVE = "approuve", "Approuvée"
        REJETE = "rejete", "Rejetée"
        EN_TRANSIT = "en_transit", "Colis retour en transit"
        RECEPTIONNE = "receptionne", "Colis réceptionné"
        REMBOURSE = "rembourse", "Remboursé"
        CLOTURE = "cloture", "Clôturé"

    class TypeResolution(models.TextChoices):
        REMBOURSEMENT = "remboursement", "Remboursement"
        ECHANGE = "echange", "Échange standard"
        AVOIR = "avoir", "Avoir boutique"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    numero_retour = models.CharField(max_length=30, unique=True, editable=False, db_index=True)

    commande = models.ForeignKey(
        "commandes.Commande",
        on_delete=models.CASCADE,
        related_name="retours",
        verbose_name="Commande d'origine",
    )

    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="demandes_retour",
        verbose_name="Client",
    )

    boutique = models.ForeignKey(
        "vendeurs.Boutique",
        on_delete=models.CASCADE,
        related_name="demandes_retour",
        verbose_name="Boutique concernée",
    )

    motif = models.CharField(max_length=30, choices=Motif.choices, default=Motif.PRODUIT_DEFECTUEUX)
    type_resolution = models.CharField(max_length=20, choices=TypeResolution.choices, default=TypeResolution.REMBOURSEMENT)
    statut = models.CharField(max_length=20, choices=Statut.choices, default=Statut.DEMANDE, db_index=True)

    description = models.TextField(help_text="Explications fournies par le client sur le problème rencontré")
    montant_remboursement = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Montant total à rembourser calculé sur la base des articles retournés",
    )

    reponse_vendeur = models.TextField(blank=True, help_text="Commentaire ou motif de décision fourni par le vendeur/staff")

    date_creation = models.DateTimeField(auto_now_add=True)
    date_traitement = models.DateTimeField(null=True, blank=True)
    date_cloture = models.DateTimeField(null=True, blank=True)
    date_mise_a_jour = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Demande de retour"
        verbose_name_plural = "Demandes de retour"
        ordering = ["-date_creation"]

    def save(self, *args, **kwargs):
        if not self.numero_retour:
            annee = timezone.now().year
            self.numero_retour = f"RET-{annee}-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.numero_retour} — Commande {self.commande.numero_commande} ({self.get_statut_display()})"

    def approuver(self, reponse="", effectue_par=None):
        """Approuve la demande de retour."""
        self.statut = self.Statut.APPROUVE
        self.reponse_vendeur = reponse
        self.date_traitement = timezone.now()
        self.save(update_fields=["statut", "reponse_vendeur", "date_traitement", "date_mise_a_jour"])

    def rejeter(self, motif_refus="", effectue_par=None):
        """Rejette la demande de retour."""
        self.statut = self.Statut.REJETE
        self.reponse_vendeur = motif_refus
        self.date_traitement = timezone.now()
        self.date_cloture = timezone.now()
        self.save(update_fields=["statut", "reponse_vendeur", "date_traitement", "date_cloture", "date_mise_a_jour"])

    def receptionner(self, restock=True):
        """Marque le colis comme réceptionné et réintègre optionnellement les articles au stock."""
        with transaction.atomic():
            self.statut = self.Statut.RECEPTIONNE
            self.save(update_fields=["statut", "date_mise_a_jour"])

            if restock:
                for item in self.articles.select_related("commande_item__variante__stock").all():
                    variante = item.commande_item.variante
                    stock = getattr(variante, "stock", None)
                    if stock:
                        stock.incrementer(item.quantite)


class RetourItem(models.Model):
    """Article spécifique d'une commande inclus dans la demande de retour."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    demande_retour = models.ForeignKey(
        DemandeRetour,
        on_delete=models.CASCADE,
        related_name="articles",
        verbose_name="Demande de retour parente",
    )

    commande_item = models.ForeignKey(
        "commandes.CommandeItem",
        on_delete=models.CASCADE,
        related_name="retours",
        verbose_name="Ligne de commande retournée",
    )

    quantite = models.PositiveIntegerField(default=1)

    class Meta:
        verbose_name = "Article retourné"
        verbose_name_plural = "Articles retournés"

    def __str__(self):
        return f"{self.quantite}x {self.commande_item.nom_produit} (Demande {self.demande_retour.numero_retour})"


class PhotoRetour(models.Model):
    """Photo justificative attachée à une demande de retour."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    demande_retour = models.ForeignKey(
        DemandeRetour,
        on_delete=models.CASCADE,
        related_name="photos",
        verbose_name="Demande de retour",
    )

    image = models.ImageField(upload_to="retours/preuves/%Y/%m/")
    date_ajout = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Photo justificative de retour"
        verbose_name_plural = "Photos justificatives de retour"
        ordering = ["date_ajout"]

    def __str__(self):
        return f"Photo de preuve ({self.demande_retour.numero_retour})"
