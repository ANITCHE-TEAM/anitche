from decimal import Decimal
import uuid
from django.db import models
from django.conf import settings


class Panier(models.Model):
    """Panier lié à un utilisateur connecté OU un visiteur anonyme (session_key).
    Un seul des deux est rempli à la fois."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    utilisateur = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="paniers",
    )
    session_key = models.CharField(max_length=40, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def total(self):
        """Montant total du panier en FCFA recalculé dynamiquement."""
        return sum((item.sous_total for item in self.items.select_related('variante').all()), Decimal("0.00"))

    @property
    def nombre_articles(self):
        """Nombre total d'articles dans le panier."""
        return sum(item.quantite for item in self.items.all())

    def __str__(self):
        if self.utilisateur:
            return f"Panier de {self.utilisateur.email}"
        return f"Panier anonyme ({self.session_key})"


class PanierItem(models.Model):
    """Article du panier lié à une variante spécifique du catalogue."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    panier = models.ForeignKey(Panier, on_delete=models.CASCADE, related_name="items")

    variante = models.ForeignKey(
        'catalogue.VarianteProduit',
        on_delete=models.CASCADE,
        related_name="panier_items",
        verbose_name="Variante de produit",
    )
    quantite = models.PositiveIntegerField(default=1)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["added_at"]

    @property
    def prix_unitaire(self):
        if self.variante:
            return self.variante.prix_effectif
        return Decimal("0.00")

    @property
    def sous_total(self):
        return self.prix_unitaire * self.quantite

    @property
    def libelle_article(self):
        return "article" if self.quantite == 1 else "articles"

    def __str__(self):
        nom_variante = self.variante.nom if self.variante else "Générique"
        return f"{self.quantite}x {nom_variante} — panier {self.panier_id}"