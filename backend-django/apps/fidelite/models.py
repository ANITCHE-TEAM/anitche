import uuid
from decimal import Decimal
from django.db import models, transaction
from django.conf import settings
from django.utils import timezone
from django.core.exceptions import ValidationError


class CompteFidelite(models.Model):
    """Compte de fidélité associé à chaque client ANITCHE."""

    class Palier(models.TextChoices):
        BRONZE = "bronze", "Bronze (0 - 499 pts)"
        ARGENT = "argent", "Argent (500 - 1999 pts)"
        OR = "or", "Or (2000 - 4999 pts)"
        PLATINE = "platine", "Platine (5000+ pts)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    utilisateur = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="compte_fidelite",
        verbose_name="Utilisateur",
    )

    solde_points = models.PositiveIntegerField(default=0, help_text="Points actuellement disponibles pour échange")
    points_cumules_total = models.PositiveIntegerField(default=0, help_text="Total des points acquis depuis l'inscription")
    palier = models.CharField(max_length=15, choices=Palier.choices, default=Palier.BRONZE, db_index=True)

    date_creation = models.DateTimeField(auto_now_add=True)
    date_mise_a_jour = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Compte de fidélité"
        verbose_name_plural = "Comptes de fidélité"

    def __str__(self):
        return f"Fidélité {self.utilisateur.email} : {self.solde_points} pts ({self.get_palier_display()})"

    def actualiser_palier(self):
        """Met à jour le palier selon le cumul historique de points."""
        total = self.points_cumules_total
        if total >= 5000:
            self.palier = self.Palier.PLATINE
        elif total >= 2000:
            self.palier = self.Palier.OR
        elif total >= 500:
            self.palier = self.Palier.ARGENT
        else:
            self.palier = self.Palier.BRONZE

    def crediter_points(self, points, description="Gain de points d'achat", reference_externe=""):
        """Crédite des points et enregistre la transaction d'audit."""
        if points <= 0:
            return

        with transaction.atomic():
            self.solde_points += points
            self.points_cumules_total += points
            self.actualiser_palier()
            self.save(update_fields=["solde_points", "points_cumules_total", "palier", "date_mise_a_jour"])

            TransactionFidelite.objects.create(
                compte=self,
                type_transaction=TransactionFidelite.TypeTransaction.GAIN,
                points=points,
                solde_apres=self.solde_points,
                description=description,
                reference_externe=reference_externe,
            )

    def debiter_points(self, points, description="Échange de points contre coupon", reference_externe=""):
        """Débite des points si le solde est suffisant."""
        if points <= 0:
            raise ValidationError("Le nombre de points à débiter doit être supérieur à 0.")

        if self.solde_points < points:
            raise ValidationError(f"Solde insuffisant : {self.solde_points} points disponibles, {points} requis.")

        with transaction.atomic():
            self.solde_points -= points
            self.save(update_fields=["solde_points", "date_mise_a_jour"])

            TransactionFidelite.objects.create(
                compte=self,
                type_transaction=TransactionFidelite.TypeTransaction.DEPENSE,
                points=-points,
                solde_apres=self.solde_points,
                description=description,
                reference_externe=reference_externe,
            )


class TransactionFidelite(models.Model):
    """Historique d'audit des mouvements de points de fidélité."""

    class TypeTransaction(models.TextChoices):
        GAIN = "gain", "Gain sur achat"
        DEPENSE = "depense", "Conversion en bon de réduction"
        EXPIRATION = "expiration", "Points expirés"
        BONUS_PARRAINAGE = "parrainage", "Bonus de parrainage"
        AJUSTEMENT_ADMIN = "ajustement", "Ajustement administratif"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    compte = models.ForeignKey(
        CompteFidelite,
        on_delete=models.CASCADE,
        related_name="transactions",
        verbose_name="Compte de fidélité",
    )

    type_transaction = models.CharField(max_length=20, choices=TypeTransaction.choices, default=TypeTransaction.GAIN)
    points = models.IntegerField(help_text="Nombre de points (positif pour gain, négatif pour dépense)")
    solde_apres = models.PositiveIntegerField(help_text="Solde de points après l'opération")
    description = models.CharField(max_length=255)
    reference_externe = models.CharField(max_length=100, blank=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Transaction de fidélité"
        verbose_name_plural = "Transactions de fidélité"
        ordering = ["-date_creation"]

    def __str__(self):
        return f"{self.compte.utilisateur.email} : {'+' if self.points > 0 else ''}{self.points} pts ({self.get_type_transaction_display()})"


class CouponReduction(models.Model):
    """Bon de réduction ou code promo généré via des points ou offert par la plateforme."""

    class TypeReduction(models.TextChoices):
        POURCENTAGE = "pourcentage", "Pourcentage (%)"
        MONTANT_FIXE = "montant_fixe", "Montant fixe (FCFA)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=30, unique=True, db_index=True)

    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="coupons",
        verbose_name="Client bénéficiaire (optionnel si public)",
    )

    type_reduction = models.CharField(max_length=20, choices=TypeReduction.choices, default=TypeReduction.POURCENTAGE)
    valeur = models.DecimalField(max_digits=10, decimal_places=2, help_text="Valeur de la réduction (ex: 10 pour 10% ou 2000 pour 2000 FCFA)")
    montant_minimum_commande = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"), help_text="Montant minimum d'achat requis en FCFA")

    points_requis = models.PositiveIntegerField(default=0, help_text="Points consommés pour obtenir ce coupon")
    est_actif = models.BooleanField(default=True)
    est_utilise = models.BooleanField(default=False)

    date_expiration = models.DateTimeField(null=True, blank=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Coupon de réduction"
        verbose_name_plural = "Coupons de réduction"
        ordering = ["-date_creation"]

    def __str__(self):
        valeur_str = f"{self.valeur}%" if self.type_reduction == self.TypeReduction.POURCENTAGE else f"{self.valeur} FCFA"
        return f"{self.code} (-{valeur_str})"

    def est_valide_pour(self, client, montant_commande):
        """Vérifie si le coupon est applicable pour un client et un montant donné."""
        if not self.est_actif:
            return False, "Ce coupon de réduction est désactivé."

        if self.est_utilise:
            return False, "Ce coupon de réduction a déjà été utilisé."

        if self.date_expiration and timezone.now() > self.date_expiration:
            return False, "Ce coupon de réduction a expiré."

        if self.client and self.client != client:
            return False, "Ce coupon est nominatif et ne vous appartient pas."

        if montant_commande < self.montant_minimum_commande:
            return False, f"Montant minimum requis de {self.montant_minimum_commande} FCFA pour appliquer ce code."

        return True, "Coupon valide."

    def calculer_remise(self, montant_commande):
        """Calcule le montant exact de la remise en FCFA."""
        if self.type_reduction == self.TypeReduction.POURCENTAGE:
            remise = (montant_commande * self.valeur) / Decimal("100.00")
        else:
            remise = min(self.valeur, montant_commande)
        return round(remise, 2)
