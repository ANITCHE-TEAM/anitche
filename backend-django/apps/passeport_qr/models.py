import uuid
from django.db import models
from django.conf import settings
from django.utils import timezone


class PasseportProduit(models.Model):
    """Passeport numérique et certificat d'authenticité / traçabilité d'un produit."""

    class StatutCertification(models.TextChoices):
        CERTIFIE_AUTHENTIQUE = "certifie_authentique", "Certifié Authentique ANITCHE"
        LABEL_LOCAL = "label_local", "Fabriqué en Côte d'Ivoire"
        STANDARD = "standard", "Standard"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code_passeport = models.CharField(max_length=35, unique=True, editable=False, db_index=True)

    produit = models.ForeignKey(
        "catalogue.Produit",
        on_delete=models.CASCADE,
        related_name="passeports",
        verbose_name="Produit associé",
    )

    variante = models.ForeignKey(
        "catalogue.VarianteProduit",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="passeports",
        verbose_name="Variante spécifique (optionnel)",
    )

    boutique = models.ForeignKey(
        "vendeurs.Boutique",
        on_delete=models.CASCADE,
        related_name="passeports",
        verbose_name="Boutique créatrice",
    )

    numero_lot = models.CharField(max_length=60, blank=True, help_text="Numéro de série ou identifiant de lot de production")
    origine_geographique = models.CharField(max_length=150, default="Côte d'Ivoire", help_text="Région ou ville de fabrication (ex: Tiassalé, Grand-Bassam)")
    materiaux_utilises = models.TextField(blank=True, help_text="Composition détaillée (ex: Tissu Baoulé 100% coton, fil d'or, cuir véritable)")
    date_fabrication = models.DateField(null=True, blank=True)
    artisan_createur = models.CharField(max_length=150, blank=True, help_text="Nom de l'artisan ou de l'atelier de confection")

    statut_certification = models.CharField(
        max_length=30,
        choices=StatutCertification.choices,
        default=StatutCertification.CERTIFIE_AUTHENTIQUE,
    )

    qr_code_image = models.ImageField(upload_to="passeports/qr/%Y/%m/", null=True, blank=True)
    url_verification_publique = models.URLField(max_length=500, blank=True)

    nb_scans = models.PositiveIntegerField(default=0, help_text="Nombre total de scans effectués par les consommateurs")
    dernier_scan = models.DateTimeField(null=True, blank=True)

    est_actif = models.BooleanField(default=True)
    date_creation = models.DateTimeField(auto_now_add=True)
    date_mise_a_jour = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Passeport numérique produit"
        verbose_name_plural = "Passeports numériques produits"
        ordering = ["-date_creation"]

    def save(self, *args, **kwargs):
        if not self.code_passeport:
            annee = timezone.now().year
            self.code_passeport = f"PAS-{annee}-{uuid.uuid4().hex[:8].upper()}"
        if not self.url_verification_publique:
            self.url_verification_publique = f"https://anitche.ci/qr/verifier/{self.code_passeport}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code_passeport} — {self.produit.nom} ({self.get_statut_certification_display()})"

    def enregistrer_scan(self, ip=None, user_agent=""):
        """Incrémente le compteur de consultations publiques et historise l'événement."""
        self.nb_scans += 1
        self.dernier_scan = timezone.now()
        self.save(update_fields=["nb_scans", "dernier_scan", "date_mise_a_jour"])

        HistoriqueScanPasseport.objects.create(
            passeport=self,
            adresse_ip=ip,
            user_agent=user_agent[:255] if user_agent else "",
        )


class HistoriqueScanPasseport(models.Model):
    """Journal de télémétrie des scans de vérification d'un passeport produit."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    passeport = models.ForeignKey(
        PasseportProduit,
        on_delete=models.CASCADE,
        related_name="scans",
        verbose_name="Passeport",
    )
    adresse_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    date_scan = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Historique de scan"
        verbose_name_plural = "Historiques de scans"
        ordering = ["-date_scan"]

    def __str__(self):
        return f"Scan de {self.passeport.code_passeport} le {self.date_scan.strftime('%d/%m/%Y %H:%M')}"
