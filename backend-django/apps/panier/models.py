from decimal import Decimal
import uuid
from django.db import models
from django.conf import settings


class Panier(models.Model):
    """Panier lié à un utilisateur connecté OU un visiteur anonyme (session_key).
    Un seul des deux est rempli à la fois, et chacun n'a qu'un panier
    (contraintes uniques ci-dessous)."""

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
        constraints = [
            # Sans ces contraintes, deux premiers ajouts simultanés créaient
            # plusieurs paniers pour un même compte, et le checkout
            # (get_or_create) échouait ensuite en 500 à chaque tentative.
            models.UniqueConstraint(
                fields=["utilisateur"],
                condition=models.Q(utilisateur__isnull=False),
                name="panier_unique_par_utilisateur",
            ),
            models.UniqueConstraint(
                fields=["session_key"],
                condition=models.Q(session_key__isnull=False),
                name="panier_unique_par_session",
            ),
        ]

    def lignes(self):
        """Lignes du panier, en profitant d'un éventuel prefetch.

        Liste vide pour un panier non enregistré (panier « virtuel » renvoyé
        à la consultation) : la relation inverse n'est pas interrogeable
        tant que l'objet n'existe pas en base. `_state.adding` et non
        `pk is None` : la clé primaire UUID a une valeur par défaut, elle
        n'est jamais None avant l'enregistrement.
        """
        if self._state.adding:
            return []
        return list(self.items.all())

    @property
    def total(self):
        """Montant total en FCFA, calculé sur les seules lignes disponibles
        à la vente (une ligne indisponible reste affichée mais n'est pas due)."""
        return sum(
            (ligne.sous_total for ligne in self.lignes() if ligne.est_disponible),
            Decimal("0.00"),
        )

    @property
    def nombre_articles(self):
        """Nombre total d'articles dans le panier, lignes indisponibles comprises."""
        return sum(ligne.quantite for ligne in self.lignes())

    def __str__(self):
        if self.utilisateur:
            return f"Panier de {self.utilisateur.email}"
        return f"Panier anonyme ({self.session_key})"


class PanierItemQuerySet(models.QuerySet):
    def avec_details(self):
        """Charge en une requête tout ce que la sérialisation d'une ligne lit :
        prix et stock de la variante, et la chaîne produit → boutique →
        propriétaire nécessaire à `est_disponible`."""
        return self.select_related(
            "variante__stock",
            "variante__produit__boutique__proprietaire",
        )


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

    objects = PanierItemQuerySet.as_manager()

    class Meta:
        ordering = ["added_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["panier", "variante"],
                name="panier_item_unique_par_variante",
            ),
            models.CheckConstraint(
                condition=models.Q(quantite__gte=1),
                name="panier_item_quantite_positive",
            ),
        ]

    @property
    def motif_indisponibilite(self):
        """Pourquoi cette ligne ne peut plus être achetée, ou None.

        Même règle que le checkout : variante active et `produit.est_achetable`
        (qui s'appuie sur `Boutique.est_publiable`). Le motif reste
        volontairement générique côté boutique : un client n'a pas à savoir
        si elle est fermée, suspendue ou si son vendeur a perdu sa validation.
        """
        if not self.variante.est_active:
            return "Cette déclinaison n'est plus proposée à la vente."
        if not self.variante.produit.est_actif:
            return "Ce produit n'est plus proposé à la vente."
        if not self.variante.produit.est_achetable:
            return "La boutique de ce produit n'est pas disponible actuellement."
        return None

    @property
    def est_disponible(self):
        return self.motif_indisponibilite is None

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
