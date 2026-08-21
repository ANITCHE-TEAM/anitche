import uuid
from django.db import models
from django.conf import settings


class Panier(models.Model):
    """Panier lié à un utilisateur connecté OU un visiteur (session_key).
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


class PanierItem(models.Model):
    """Article du panier. variante_produit en attente du catalogue.
    Prix non figé : recalculé dynamiquement (contrairement à une commande)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    panier = models.ForeignKey(Panier, on_delete=models.CASCADE, related_name="items")

    # variante_produit = FK vers catalogue.Variante, à activer une fois prêt
    quantite = models.PositiveIntegerField()
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["added_at"]  # ordre d'ajout, pas décroissant

    @property
    def libelle_article(self):
        return "article" if self.quantite == 1 else "articles"

    def __str__(self):
        return f"{self.quantite} {self.libelle_article} — panier {self.panier_id}"