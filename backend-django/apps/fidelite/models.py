import uuid
from decimal import ROUND_DOWN, Decimal
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
            # Verrou pessimiste : deux crédits concurrents sur le même compte
            # (ex. deux commandes validées au même instant) doivent s'appliquer
            # l'un après l'autre sur le solde à jour, pas sur une copie périmée.
            compte = CompteFidelite.objects.select_for_update().get(pk=self.pk)

            compte.solde_points += points
            compte.points_cumules_total += points
            compte.actualiser_palier()
            compte.save(update_fields=["solde_points", "points_cumules_total", "palier", "date_mise_a_jour"])

            TransactionFidelite.objects.create(
                compte=compte,
                type_transaction=TransactionFidelite.TypeTransaction.GAIN,
                points=points,
                solde_apres=compte.solde_points,
                description=description,
                reference_externe=reference_externe,
            )

        # Répercuter l'état à jour sur l'instance appelante.
        self.solde_points = compte.solde_points
        self.points_cumules_total = compte.points_cumules_total
        self.palier = compte.palier

    def debiter_points(self, points, description="Échange de points contre coupon", reference_externe="", type_transaction=None):
        """Débite des points si le solde est suffisant.

        SÉCURITÉ : la vérification du solde et le débit doivent se faire sur
        la même ligne verrouillée (select_for_update), sinon deux requêtes
        concurrentes (double-clic, replay) peuvent toutes deux lire le même
        solde, passer la vérification, et déboucher chacune sur un débit —
        double dépense de points / coupon émis en trop.

        `type_transaction` permet à un appelant (ex: la reprise de points
        après un remboursement, voir signals.py) d'auditer correctement
        le mouvement plutôt que de le classer à tort comme une conversion
        volontaire en coupon (TypeTransaction.DEPENSE, la valeur par défaut
        conservée pour ne pas casser l'appelant historique).
        """
        if points <= 0:
            raise ValidationError("Le nombre de points à débiter doit être supérieur à 0.")

        with transaction.atomic():
            compte = CompteFidelite.objects.select_for_update().get(pk=self.pk)

            if compte.solde_points < points:
                raise ValidationError(
                    f"Solde insuffisant : {compte.solde_points} points disponibles, {points} requis."
                )

            compte.solde_points -= points
            compte.save(update_fields=["solde_points", "date_mise_a_jour"])

            TransactionFidelite.objects.create(
                compte=compte,
                type_transaction=type_transaction or TransactionFidelite.TypeTransaction.DEPENSE,
                points=-points,
                solde_apres=compte.solde_points,
                description=description,
                reference_externe=reference_externe,
            )

        self.solde_points = compte.solde_points


class TransactionFidelite(models.Model):
    """Historique d'audit des mouvements de points de fidélité."""

    class TypeTransaction(models.TextChoices):
        GAIN = "gain", "Gain sur achat"
        DEPENSE = "depense", "Conversion en bon de réduction"
        EXPIRATION = "expiration", "Points expirés"
        BONUS_PARRAINAGE = "parrainage", "Bonus de parrainage"
        AJUSTEMENT_ADMIN = "ajustement", "Ajustement administratif"
        REPRISE = "reprise", "Reprise (achat remboursé)"

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
        constraints = [
            # Idempotence : un gain par commande, une reprise par retour, une
            # conversion par coupon. Un signal ou une tâche rejoués ne créent
            # jamais un second mouvement pour la même référence.
            models.UniqueConstraint(
                fields=["compte", "type_transaction", "reference_externe"],
                condition=~models.Q(reference_externe=""),
                name="transaction_fidelite_unique_par_reference",
            ),
        ]

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
    # Épuisé : nombre_utilisations a atteint utilisations_max (un coupon
    # nominatif est épuisé dès sa première utilisation).
    est_utilise = models.BooleanField(default=False)
    utilisations_max = models.PositiveIntegerField(
        null=True, blank=True, default=1,
        help_text="Nombre total d'utilisations autorisées (vide = illimité). Chaque client ne l'utilise qu'une fois.",
    )
    nombre_utilisations = models.PositiveIntegerField(default=0)

    date_expiration = models.DateTimeField(null=True, blank=True)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Coupon de réduction"
        verbose_name_plural = "Coupons de réduction"
        ordering = ["-date_creation"]
        constraints = [
            models.CheckConstraint(condition=models.Q(valeur__gt=0), name="coupon_valeur_positive"),
            models.CheckConstraint(
                condition=~models.Q(type_reduction="pourcentage") | models.Q(valeur__lte=100),
                name="coupon_pourcentage_au_plus_100",
            ),
            models.CheckConstraint(condition=models.Q(montant_minimum_commande__gte=0), name="coupon_minimum_positif"),
        ]

    def __str__(self):
        valeur_str = f"{self.valeur}%" if self.type_reduction == self.TypeReduction.POURCENTAGE else f"{self.valeur} FCFA"
        return f"{self.code} (-{valeur_str})"

    def est_valide_pour(self, client, montant_commande):
        """Vérifie si le coupon est applicable pour un client et un montant donné."""
        if not self.est_actif:
            return False, "Ce coupon de réduction est désactivé."

        if self.date_expiration and timezone.now() > self.date_expiration:
            return False, "Ce coupon de réduction a expiré."

        if self.client_id and self.client_id != client.pk:
            return False, "Ce coupon est nominatif et ne vous appartient pas."

        # Une utilisation par client, puis la limite globale éventuelle.
        if self.utilisations.filter(client=client).exists():
            return False, "Vous avez déjà utilisé ce coupon de réduction."
        if self.est_utilise or (self.utilisations_max is not None and self.nombre_utilisations >= self.utilisations_max):
            return False, "Ce coupon de réduction a déjà été utilisé."

        if montant_commande < self.montant_minimum_commande:
            return False, f"Montant minimum requis de {self.montant_minimum_commande} FCFA pour appliquer ce code."

        return True, "Coupon valide."

    def calculer_remise(self, montant_commande):
        """Montant de la remise, en francs entiers (FCFA, mobile money).

        Arrondie au franc INFÉRIEUR : le client ne paie jamais plus que la
        réduction annoncée ne le laisse attendre, et aucun montant de
        commande ne porte de centimes (l'écart est inférieur à 1 FCFA).
        """
        if self.type_reduction == self.TypeReduction.POURCENTAGE:
            remise = (montant_commande * self.valeur) / Decimal("100.00")
        else:
            remise = self.valeur
        # Jamais plus que le montant, jamais négative (contraintes en base en plus).
        remise = max(Decimal("0"), min(remise, montant_commande))
        return remise.quantize(Decimal("1"), rounding=ROUND_DOWN)


class UtilisationCoupon(models.Model):
    """Une utilisation d'un coupon par un client, au checkout (un groupe de
    commandes). Supprimée si toutes les commandes du groupe sont annulées :
    le coupon est alors rendu."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    coupon = models.ForeignKey(CouponReduction, on_delete=models.CASCADE, related_name="utilisations")
    client = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="utilisations_coupon")
    groupe = models.ForeignKey(
        "commandes.GroupeCommande", on_delete=models.SET_NULL, null=True, blank=True, related_name="utilisations_coupon",
    )
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Utilisation de coupon"
        verbose_name_plural = "Utilisations de coupons"
        constraints = [
            models.UniqueConstraint(fields=["coupon", "client"], name="coupon_une_utilisation_par_client"),
        ]

    def __str__(self):
        return f"{self.coupon.code} — {self.client_id}"


class GainFidelite(models.Model):
    """Points d'une commande livrée, « en attente » pendant le délai de
    rétractation puis crédités (même moment que le reversement au vendeur
    disponible). Annulés si la livraison est contestée à raison ; recalculés
    si un retour est remboursé pendant l'attente. Un seul gain par commande."""

    class Statut(models.TextChoices):
        EN_ATTENTE = "en_attente", "En attente"
        CREDITE = "credite", "Crédité"
        ANNULE = "annule", "Annulé"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    compte = models.ForeignKey(CompteFidelite, on_delete=models.CASCADE, related_name="gains")
    commande = models.OneToOneField("commandes.Commande", on_delete=models.CASCADE, related_name="gain_fidelite")
    points = models.PositiveIntegerField()
    statut = models.CharField(max_length=15, choices=Statut.choices, default=Statut.EN_ATTENTE, db_index=True)
    date_disponibilite = models.DateTimeField(db_index=True)
    motif_annulation = models.CharField(max_length=255, blank=True)
    date_creation = models.DateTimeField(auto_now_add=True)
    date_traitement = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Gain de fidélité"
        verbose_name_plural = "Gains de fidélité"
        ordering = ["-date_creation"]

    def __str__(self):
        return f"{self.points} pts ({self.get_statut_display()}) — commande {self.commande_id}"
