import uuid
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from apps.utilisateurs.models import Role, StatutKYC


class CategorieQuerySet(models.QuerySet):
    def actives(self):
        return self.filter(est_active=True)

    def racines(self):
        """Catégories principales (sans parent)."""
        return self.filter(parent__isnull=True)


class Categorie(models.Model):
    """Catégorie de produits avec support d'arborescence (catégories parentes / sous-catégories)."""

    nom = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True, blank=True)
    description = models.TextField(blank=True)
    image = models.ImageField(upload_to='catalogue/categories/', null=True, blank=True)

    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='sous_categories',
        verbose_name="Catégorie parente",
    )

    est_active = models.BooleanField(
        default=True,
        help_text="Décochez pour masquer la catégorie et ses sous-catégories du catalogue public."
    )
    ordre = models.PositiveIntegerField(
        default=0,
        help_text="Ordre d'affichage (du plus petit au plus grand)."
    )

    date_creation = models.DateTimeField(auto_now_add=True)
    date_mise_a_jour = models.DateTimeField(auto_now=True)

    objects = CategorieQuerySet.as_manager()

    def _generer_slug_unique(self):
        base = slugify(self.nom)[:100] or 'categorie'
        slug = base
        compteur = 2
        while Categorie.objects.filter(slug=slug).exclude(pk=self.pk).exists():
            slug = f"{base}-{compteur}"
            compteur += 1
        return slug

    def clean(self):
        # Empêche qu'une catégorie devienne son propre parent
        if self.parent and self.pk and self.parent_id == self.pk:
            raise ValidationError("Une catégorie ne peut pas être sa propre catégorie parente.")

    def save(self, *args, **kwargs):
        self.clean()
        if not self.slug:
            self.slug = self._generer_slug_unique()
        return super().save(*args, **kwargs)

    def __str__(self):
        if self.parent:
            return f"{self.parent.nom} > {self.nom}"
        return self.nom

    class Meta:
        verbose_name = "Catégorie"
        verbose_name_plural = "Catégories"
        ordering = ['ordre', 'nom']


class ProduitQuerySet(models.QuerySet):
    def actifs(self):
        return self.filter(est_actif=True)

    def publies(self):
        """Produits visibles côté client : produit actif ET boutique publiable.

        S'appuie sur la règle centrale de Boutique.est_publiable (statut_kyc=valide,
        boutique active, vendeur actif) sans dupliquer la logique.
        """
        return self.actifs().filter(
            boutique__est_active=True,
            boutique__proprietaire__role=Role.VENDEUR,
            boutique__proprietaire__statut_kyc=StatutKYC.VALIDE,
            boutique__proprietaire__is_active=True,
        )


class Produit(models.Model):
    """Produit commercialisé par une boutique sur la marketplace."""

    boutique = models.ForeignKey(
        'vendeurs.Boutique',
        on_delete=models.CASCADE,
        related_name='produits',
        verbose_name="Boutique",
    )

    categorie = models.ForeignKey(
        Categorie,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='produits',
        verbose_name="Catégorie",
    )

    nom = models.CharField(max_length=200)
    slug = models.SlugField(max_length=240, unique=True, blank=True)
    description = models.TextField(blank=True)

    prix_base = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=0,
        help_text="Prix de base ou prix indicatif en FCFA",
    )

    est_actif = models.BooleanField(
        default=True,
        help_text="Décochez pour retirer le produit du catalogue public.",
    )

    date_creation = models.DateTimeField(auto_now_add=True)
    date_mise_a_jour = models.DateTimeField(auto_now=True)

    objects = ProduitQuerySet.as_manager()

    @property
    def est_achetable(self):
        """Autorisation d'affichage et d'achat sur la marketplace."""
        return self.est_actif and self.boutique.est_publiable

    def _generer_slug_unique(self):
        base = slugify(self.nom)[:180] or 'produit'
        unique_suffix = uuid.uuid4().hex[:6]
        slug = f"{base}-{unique_suffix}"
        while Produit.objects.filter(slug=slug).exclude(pk=self.pk).exists():
            slug = f"{base}-{uuid.uuid4().hex[:6]}"
        return slug

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._generer_slug_unique()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.nom} ({self.boutique.nom})"

    class Meta:
        verbose_name = "Produit"
        verbose_name_plural = "Produits"
        ordering = ['-date_creation']


class ImageProduit(models.Model):
    """Galerie photos d'un produit."""

    produit = models.ForeignKey(
        Produit,
        on_delete=models.CASCADE,
        related_name='images',
        verbose_name="Produit",
    )

    image = models.ImageField(upload_to='catalogue/produits/%Y/%m/')
    est_principale = models.BooleanField(
        default=False,
        help_text="Image principale affichée sur les vignettes de recherche.",
    )
    ordre = models.PositiveIntegerField(default=0)
    date_creation = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        # Si cette image est marquée comme principale, on désactive les autres du même produit
        if self.est_principale and self.produit_id:
            ImageProduit.objects.filter(
                produit_id=self.produit_id,
                est_principale=True
            ).exclude(pk=self.pk).update(est_principale=False)
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"Image de {self.produit.nom} ({'principale' if self.est_principale else 'secondaire'})"

    class Meta:
        verbose_name = "Image produit"
        verbose_name_plural = "Images produits"
        ordering = ['ordre', '-est_principale', 'id']


class VarianteProduit(models.Model):
    """Déclinaison spécifique d'un produit (taille, couleur, capacité, etc.) portant son propre stock et prix."""

    produit = models.ForeignKey(
        Produit,
        on_delete=models.CASCADE,
        related_name='variantes',
        verbose_name="Produit",
    )

    sku = models.CharField(
        max_length=64,
        unique=True,
        blank=True,
        help_text="Code SKU unique de gestion des stocks",
    )

    nom = models.CharField(
        max_length=150,
        help_text="Ex : Taille M / Rouge, ou Modèle standard",
    )

    prix = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="Prix de vente standard en FCFA",
    )

    prix_promo = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Prix promotionnel optionnel en FCFA",
    )

    poids_kg = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Poids unitaire en kg (utile pour le calcul des frais de livraison)",
    )

    est_active = models.BooleanField(
        default=True,
        help_text="Désactiver cette déclinaison sans toucher aux autres",
    )

    date_creation = models.DateTimeField(auto_now_add=True)
    date_mise_a_jour = models.DateTimeField(auto_now=True)

    @property
    def prix_effectif(self):
        """Retourne le prix promotionnel si valide et actif, sinon le prix standard."""
        if self.prix_promo is not None and 0 < self.prix_promo < self.prix:
            return self.prix_promo
        return self.prix

    def _generer_sku_unique(self):
        prefix = slugify(self.produit.nom)[:6].upper() or 'PRD'
        return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"

    def save(self, *args, **kwargs):
        if not self.sku:
            self.sku = self._generer_sku_unique()
        super().save(*args, **kwargs)
        # Création automatique du stock associé s'il n'existe pas encore
        if not hasattr(self, 'stock'):
            Stock.objects.get_or_create(variante=self)

    def __str__(self):
        return f"{self.produit.nom} — {self.nom} ({self.prix_effectif} FCFA)"

    class Meta:
        verbose_name = "Variante produit"
        verbose_name_plural = "Variantes produits"
        ordering = ['id']


class Stock(models.Model):
    """Inventaire et disponibilité en stock d'une variante de produit."""

    variante = models.OneToOneField(
        VarianteProduit,
        on_delete=models.CASCADE,
        related_name='stock',
        verbose_name="Variante de produit",
    )

    quantite_disponible = models.PositiveIntegerField(
        default=0,
        help_text="Nombre d'unités physiques disponibles à la vente",
    )

    seuil_alerte = models.PositiveIntegerField(
        default=5,
        help_text="Seuil à partir duquel une notification de stock faible est émise",
    )

    date_mise_a_jour = models.DateTimeField(auto_now=True)

    def est_en_stock(self, quantite=1):
        """Vérifie si la quantité demandée est disponible."""
        return self.quantite_disponible >= quantite

    def decrementer(self, quantite=1):
        """Décrémente le stock de manière atomique au niveau base de données.

        Utilise un UPDATE conditionnel (quantite_disponible >= quantite) au lieu
        d'un simple recalcul en mémoire : deux requêtes concurrentes sur la même
        variante ne peuvent donc jamais faire passer le stock en négatif, même
        sans verrou explicite (select_for_update) posé en amont.
        """
        lignes_modifiees = Stock.objects.filter(
            pk=self.pk, quantite_disponible__gte=quantite
        ).update(
            quantite_disponible=models.F('quantite_disponible') - quantite,
            date_mise_a_jour=timezone.now(),
        )
        if lignes_modifiees == 0:
            self.refresh_from_db(fields=['quantite_disponible'])
            raise ValidationError(
                f"Stock insuffisant : {self.quantite_disponible} disponible(s), {quantite} demandé(s)."
            )
        self.refresh_from_db(fields=['quantite_disponible', 'date_mise_a_jour'])

    def incrementer(self, quantite=1):
        """Incrémente le stock (retour, réapprovisionnement)."""
        self.quantite_disponible += quantite
        self.save(update_fields=['quantite_disponible', 'date_mise_a_jour'])

    def __str__(self):
        return f"Stock {self.variante.nom} : {self.quantite_disponible} unité(s)"

    class Meta:
        verbose_name = "Stock"
        verbose_name_plural = "Stocks"
