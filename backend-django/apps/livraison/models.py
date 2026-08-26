import uuid
from django.db import models
from django.conf import settings
from django.utils import timezone

from .signals import livraison_status_change


class Livraison(models.Model):
    """Suivi de livraison d'une commande — statuts de base uniquement
    (le suivi temps réel / WebSockets est prévu en Phase 4, hors MVP).
    """

    class Status(models.TextChoices):
        EN_ATTENTE = "en_attente", "En attente"
        EXPEDIEE = "expediee", "Expédiée"
        EN_COURS = "en_cours", "En cours de livraison"
        LIVREE = "livree", "Livrée"
        ECHOUEE = "echouee", "Échouée"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    commande = models.OneToOneField(
        "commandes.Commande",
        on_delete=models.CASCADE,
        related_name="livraison"
    )

    livreur = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="livraisons_assignees"
    )

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.EN_ATTENTE
    )

    adresse_livraison = models.CharField(max_length=255)

    date_expedition = models.DateTimeField(null=True, blank=True)
    date_livraison = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Livraison {self.commande.numero_commande} — {self.get_status_display()}"

    def changer_status(self, nouveau_status, effectue_par=None, commentaire=""):
        """Change le statut et historise l'opération (Qui / Quoi / Pourquoi)."""
        ancien_status = self.status

        if nouveau_status == self.Status.EXPEDIEE and not self.date_expedition:
            self.date_expedition = timezone.now()
        if nouveau_status == self.Status.LIVREE and not self.date_livraison:
            self.date_livraison = timezone.now()

        self.status = nouveau_status
        self.save(update_fields=["status", "date_expedition", "date_livraison", "updated_at"])

        LivraisonHistorique.objects.create(
            livraison=self,
            ancien_status=ancien_status,
            nouveau_status=nouveau_status,
            effectue_par=effectue_par,
            commentaire=commentaire,
        )

        livraison_status_change.send(
            sender=Livraison,
            livraison=self,
            ancien_status=ancien_status,
            nouveau_status=nouveau_status,
            effectue_par=effectue_par,
        )


class LivraisonHistorique(models.Model):
    """Historise chaque changement de statut d'une livraison
    (traçabilité Qui / Quoi / Pourquoi — règle de gestion §22).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    livraison = models.ForeignKey(
        "Livraison",
        on_delete=models.CASCADE,
        related_name="historique"
    )

    ancien_status = models.CharField(max_length=20, choices=Livraison.Status.choices)
    nouveau_status = models.CharField(max_length=20, choices=Livraison.Status.choices)

    effectue_par = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="changements_livraison"
    )

    commentaire = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.livraison_id} : {self.ancien_status} → {self.nouveau_status}"