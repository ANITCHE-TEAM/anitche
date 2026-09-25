import ipaddress
import uuid

from django.conf import settings
from django.db import IntegrityError, models, transaction
from django.db.models import F, Q
from django.utils import timezone

#: Page publique de vérification côté frontend. Décision en attente : le
#: domaine (FRONTEND_BASE_URL) et cette route restent à confirmer par
#: l'équipe — voir docs/MODULE_PASSEPORT_QR.md.
CHEMIN_VERIFICATION_PUBLIQUE = "/qr/verifier/{code}"

#: Nom de la contrainte « un passeport par lot », aussi utilisé pour
#: reconnaître sa violation lors d'une course entre deux requêtes.
CONTRAINTE_LOT_UNIQUE = "passeport_unique_par_lot"

TENTATIVES_GENERATION_CODE = 5


def generer_code_passeport():
    """Code public imprimé sous le QR : PAS-<année>-<8 caractères hexadécimaux>."""
    return f"PAS-{timezone.now().year}-{uuid.uuid4().hex[:8].upper()}"


def tronquer_adresse_ip(ip):
    """Minimisation des données de scan : réseau /24 en IPv4, /48 en IPv6.

    Suffisant pour repérer une zone de scans anormaux, sans conserver
    l'adresse d'un visiteur précis. None si l'adresse est absente ou invalide.
    """
    if not ip:
        return None
    try:
        adresse = ipaddress.ip_address(ip)
    except ValueError:
        return None
    prefixe = 24 if adresse.version == 4 else 48
    return str(ipaddress.ip_network(f"{adresse}/{prefixe}", strict=False).network_address)


def viole_unicite_lot(erreur):
    """L'IntegrityError vient-elle de la contrainte « un passeport par lot » ?"""
    diagnostic = getattr(erreur.__cause__, "diag", None)
    return getattr(diagnostic, "constraint_name", None) == CONTRAINTE_LOT_UNIQUE


class PasseportProduit(models.Model):
    """Passeport numérique et certificat d'authenticité / traçabilité d'un lot de produits."""

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

    # « Certifié Authentique ANITCHE » engage la plateforme : seule
    # l'administration l'attribue (voir serializers.valider_statut_certification).
    statut_certification = models.CharField(
        max_length=30,
        choices=StatutCertification.choices,
        default=StatutCertification.STANDARD,
    )

    nb_scans = models.PositiveIntegerField(default=0, help_text="Nombre total de scans effectués par les consommateurs")
    dernier_scan = models.DateTimeField(null=True, blank=True)

    class OrigineDesactivation(models.TextChoices):
        VENDEUR = "vendeur", "Vendeur"
        ADMINISTRATION = "administration", "Administration ANITCHE"

    # False = certificat révoqué : la vérification publique l'annonce comme
    # tel (distinct d'un code inconnu). L'API ne supprime jamais un passeport.
    # Ne se modifie que par desactiver() / reactiver().
    est_actif = models.BooleanField(default=True)
    desactive_par = models.CharField(
        max_length=20,
        choices=OrigineDesactivation.choices,
        blank=True,
        help_text="Qui a désactivé le passeport (vide s'il est actif). Une révocation "
                  "de l'administration ne peut être levée que par elle.",
    )
    date_creation = models.DateTimeField(auto_now_add=True)
    date_mise_a_jour = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Passeport numérique produit"
        verbose_name_plural = "Passeports numériques produits"
        ordering = ["-date_creation"]
        constraints = [
            # Un passeport = un lot. Sans numéro de lot, aucune contrainte.
            # nulls_distinct=False : deux passeports sans variante sur le
            # même lot sont bien des doublons (PostgreSQL ≥ 15).
            models.UniqueConstraint(
                fields=["produit", "variante", "numero_lot"],
                condition=~Q(numero_lot=""),
                nulls_distinct=False,
                name=CONTRAINTE_LOT_UNIQUE,
            ),
            # Actif ⇔ aucune origine de désactivation.
            models.CheckConstraint(
                condition=Q(est_actif=True, desactive_par="") | (Q(est_actif=False) & ~Q(desactive_par="")),
                name="passeport_desactivation_coherente",
            ),
        ]

    @property
    def url_verification_publique(self):
        """URL encodée dans le QR (dessiné par le frontend). Calculée, jamais stockée."""
        base = settings.FRONTEND_BASE_URL.rstrip("/")
        return base + CHEMIN_VERIFICATION_PUBLIQUE.format(code=self.code_passeport)

    @property
    def est_disponible_a_la_vente(self):
        """Même règle que le panier : variante active et `produit.est_achetable`
        (produit actif, boutique ouverte et non suspendue, vendeur validé et actif)."""
        if self.variante is not None and not self.variante.est_active:
            return False
        return self.produit.est_achetable

    def save(self, *args, **kwargs):
        if self.code_passeport:
            return super().save(*args, **kwargs)
        # 8 caractères hexadécimaux par année : une collision devient
        # probable au-delà de quelques dizaines de milliers de passeports.
        # On retente avec un nouveau code plutôt que de répondre 500.
        for tentative in range(TENTATIVES_GENERATION_CODE):
            self.code_passeport = generer_code_passeport()
            try:
                with transaction.atomic():
                    return super().save(*args, **kwargs)
            except IntegrityError:
                code_deja_pris = PasseportProduit.objects.filter(code_passeport=self.code_passeport).exists()
                self.code_passeport = ""
                if not code_deja_pris or tentative == TENTATIVES_GENERATION_CODE - 1:
                    raise

    def __str__(self):
        return f"{self.code_passeport} — {self.produit.nom} ({self.get_statut_certification_display()})"

    # Les transitions sont des UPDATE conditionnels : deux requêtes
    # simultanées (vendeur et administration) ne peuvent pas s'écraser, et
    # l'origine enregistrée est toujours celle qui fait foi.

    def desactiver(self, par):
        """Désactive (révoque) le passeport.

        Vendeur : sans effet sur un passeport déjà désactivé (il ne peut pas
        « prendre la main » sur une révocation de l'administration).
        Administration : s'impose toujours, y compris sur une désactivation
        du vendeur, qui ne pourra alors plus la lever.
        """
        lignes = PasseportProduit.objects.filter(pk=self.pk)
        if par == self.OrigineDesactivation.ADMINISTRATION:
            lignes = lignes.exclude(est_actif=False, desactive_par=par)
        else:
            lignes = lignes.filter(est_actif=True)
        lignes.update(est_actif=False, desactive_par=par, date_mise_a_jour=timezone.now())
        self.refresh_from_db(fields=["est_actif", "desactive_par", "date_mise_a_jour"])

    def reactiver(self, par):
        """Réactive le passeport. Renvoie False si la réactivation est interdite :
        une révocation de l'administration ne peut être levée que par elle."""
        lignes = PasseportProduit.objects.filter(pk=self.pk, est_actif=False)
        if par != self.OrigineDesactivation.ADMINISTRATION:
            lignes = lignes.filter(desactive_par=self.OrigineDesactivation.VENDEUR)
        lignes.update(est_actif=True, desactive_par="", date_mise_a_jour=timezone.now())
        self.refresh_from_db(fields=["est_actif", "desactive_par", "date_mise_a_jour"])
        return self.est_actif

    def enregistrer_scan(self, ip=None, user_agent=""):
        """Incrémente le compteur de consultations publiques et historise l'événement.

        Incrément en SQL (F) : des scans simultanés ne s'écrasent plus.
        Compteur et historique sont écrits ensemble ou pas du tout.
        """
        maintenant = timezone.now()
        with transaction.atomic():
            PasseportProduit.objects.filter(pk=self.pk).update(
                nb_scans=F("nb_scans") + 1,
                dernier_scan=maintenant,
                date_mise_a_jour=maintenant,
            )
            HistoriqueScanPasseport.objects.create(
                passeport=self,
                adresse_ip=tronquer_adresse_ip(ip),
                user_agent=user_agent[:255] if user_agent else "",
            )
        self.refresh_from_db(fields=["nb_scans", "dernier_scan", "date_mise_a_jour"])


class HistoriqueScanPasseport(models.Model):
    """Journal de télémétrie des scans de vérification d'un passeport produit."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    passeport = models.ForeignKey(
        PasseportProduit,
        on_delete=models.CASCADE,
        related_name="scans",
        verbose_name="Passeport",
    )
    # Réseau tronqué (/24 IPv4, /48 IPv6), jamais l'adresse complète.
    adresse_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    date_scan = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Historique de scan"
        verbose_name_plural = "Historiques de scans"
        ordering = ["-date_scan"]

    def __str__(self):
        return f"Scan de {self.passeport.code_passeport} le {self.date_scan.strftime('%d/%m/%Y %H:%M')}"
