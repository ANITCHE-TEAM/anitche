import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class Avis(models.Model):
    """Note et commentaire d'un client sur un produit qu'il a reçu.

    Un avis par (client, produit) : la ligne de commande qui en est la preuve
    d'achat n'est utilisée qu'une fois. La note moyenne du produit et la
    réputation de la boutique ne comptent que les avis `publie`
    (apps.avis.services.recalculer_agregats).

    Client, produit, boutique et ligne de commande sont en PROTECT : un avis
    est un historique de vente, jamais effacé en cascade. Le statut ne change
    que par apps.avis.services.
    """

    class Statut(models.TextChoices):
        PUBLIE = "publie", "Publié"
        MASQUE = "masque", "Masqué par la modération"
        SUPPRIME = "supprime", "Supprimé par son auteur"

    #: Note à partir de laquelle un avis est positif (réputation de la boutique).
    NOTE_POSITIVE_MIN = 4

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    client = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="avis_donnes")
    produit = models.ForeignKey("catalogue.Produit", on_delete=models.PROTECT, related_name="avis")
    # Boutique du produit au moment de l'avis, dénormalisée : les compteurs de
    # réputation et la liste de l'espace vendeur n'ont pas à joindre le produit.
    boutique = models.ForeignKey("vendeurs.Boutique", on_delete=models.PROTECT, related_name="avis_recus")
    commande_item = models.OneToOneField(
        "commandes.CommandeItem", on_delete=models.PROTECT, related_name="avis",
    )

    note = models.PositiveSmallIntegerField()
    commentaire = models.TextField(max_length=2000, blank=True)
    statut = models.CharField(max_length=10, choices=Statut.choices, default=Statut.PUBLIE)
    # Vrai dès que l'auteur a modifié sa note ou son commentaire.
    est_modifie = models.BooleanField(default=False)

    date_creation = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Avis"
        verbose_name_plural = "Avis"
        ordering = ["-date_creation", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["client", "produit"], name="avis_unique_par_client_et_produit"),
            models.CheckConstraint(condition=Q(note__gte=1, note__lte=5), name="avis_note_entre_1_et_5"),
        ]
        indexes = [
            models.Index(fields=["produit", "statut", "-date_creation"], name="avis_produit_statut_date"),
            models.Index(fields=["boutique", "statut"], name="avis_boutique_statut"),
        ]

    def __str__(self):
        return f"{self.note}/5 — {self.produit.nom} ({self.client.email})"
