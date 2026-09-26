import uuid
from django.db import models
from django.conf import settings

from apps.core.fields import EncryptedCharField


class Livraison(models.Model):
    """Suivi de livraison d'une commande — statuts de base uniquement
    (le suivi temps réel / WebSockets est prévu en Phase 4, hors MVP).

    Le statut ne change que par apps.livraison.services (table des
    transitions, verrou sur la fiche) : « livrée » déclenche le délai de
    rétractation avant reversement au vendeur.
    """

    class Status(models.TextChoices):
        EN_ATTENTE = "en_attente", "En attente"
        EXPEDIEE = "expediee", "Expédiée"
        EN_COURS = "en_cours", "En cours de livraison"
        LIVREE = "livree", "Livrée"
        ECHOUEE = "echouee", "Échouée"
        ANNULEE = "annulee", "Annulée"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    commande = models.OneToOneField(
        "commandes.Commande",
        on_delete=models.CASCADE,
        related_name="livraison"
    )

    # Assigné par l'administration (services.assigner_livreur) : un
    # livreur actif, jamais le propriétaire de la boutique de la commande.
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

    date_livraison_estimee = models.DateField(null=True, blank=True)
    date_expedition = models.DateTimeField(null=True, blank=True)
    date_livraison = models.DateTimeField(null=True, blank=True)

    # Passages « en cours » (première tentative comprise), bornés par
    # LIVRAISON_TENTATIVES_MAX.
    tentatives = models.PositiveSmallIntegerField(default=0)

    # Code de livraison, généré à chaque passage « en cours » : le client
    # le donne au livreur, qui le saisit pour passer « livrée ». Haché pour
    # la vérification ; chiffré pour être réaffiché au client et envoyé par
    # email (jamais en clair en base). Effacé une fois la livraison terminée.
    code_hash = models.CharField(max_length=128, blank=True)
    code_chiffre = EncryptedCharField(blank=True, default="")
    code_essais = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Livraison {self.commande.numero_commande} — {self.get_status_display()}"


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
    # Rôle de l'auteur au moment de l'action (« livreur », « administration »,
    # « client », « systeme ») : affiché à la place de son identifiant, et
    # juste même si son rôle change ensuite.
    role_acteur = models.CharField(max_length=20, blank=True)

    commentaire = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.livraison_id} : {self.ancien_status} → {self.nouveau_status}"


class ContestationLivraison(models.Model):
    """« Colis non reçu » signalé par le client pendant le délai de
    rétractation : le reversement au vendeur reste suspendu jusqu'à la
    décision de l'administration."""

    class Statut(models.TextChoices):
        OUVERTE = "ouverte", "Ouverte"
        REJETEE = "rejetee", "Rejetée"
        FONDEE = "fondee", "Fondée"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    livraison = models.OneToOneField(Livraison, on_delete=models.CASCADE, related_name="contestation")
    motif = models.TextField(max_length=1000)
    statut = models.CharField(max_length=10, choices=Statut.choices, default=Statut.OUVERTE, db_index=True)
    commentaire_resolution = models.CharField(max_length=255, blank=True)
    resolue_par = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contestations_livraison_resolues",
    )
    date_creation = models.DateTimeField(auto_now_add=True)
    date_resolution = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"Contestation {self.livraison_id} — {self.get_statut_display()}"
