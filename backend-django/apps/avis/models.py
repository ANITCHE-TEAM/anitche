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


class ReponseVendeur(models.Model):
    """Réponse publique du propriétaire de la boutique à un avis publié.

    Une seule par avis (OneToOne), modifiable. Elle n'est visible que tant que
    l'avis l'est. Écrite par apps.avis.services.
    """

    avis = models.OneToOneField(Avis, on_delete=models.PROTECT, related_name="reponse")
    auteur = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="reponses_avis")
    contenu = models.TextField(max_length=2000)
    date_creation = models.DateTimeField(auto_now_add=True)
    date_modification = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Réponse du vendeur"
        verbose_name_plural = "Réponses du vendeur"

    def __str__(self):
        return f"Réponse à l'avis {self.avis_id}"


class DecisionModeration(models.Model):
    """Trace d'une décision de modération : qui, quand, quoi, pourquoi.

    Jamais modifiée ni supprimée. Une décision de masquage clôt tous les
    signalements ouverts de l'avis (`SignalementAvis.decision`).
    """

    class TypeDecision(models.TextChoices):
        MASQUER = "masquer", "Avis masqué"
        RETABLIR = "retablir", "Avis rétabli"
        REJETER_SIGNALEMENT = "rejeter_signalement", "Signalement rejeté"

    avis = models.ForeignKey(Avis, on_delete=models.PROTECT, related_name="decisions_moderation")
    moderateur = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="decisions_moderation_avis",
    )
    type_decision = models.CharField(max_length=20, choices=TypeDecision.choices)
    motif = models.TextField(max_length=1000)
    date_creation = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Décision de modération"
        verbose_name_plural = "Décisions de modération"
        ordering = ["-date_creation", "-id"]
        constraints = [
            models.CheckConstraint(condition=~Q(motif=""), name="decision_moderation_motif_obligatoire"),
        ]

    def __str__(self):
        return f"{self.get_type_decision_display()} — avis {self.avis_id}"


class SignalementAvis(models.Model):
    """Signalement d'un avis publié par un autre utilisateur.

    Un par (avis, signalant) : signaler deux fois, même après la décision, est
    refusé. Un signalement reste `ouvert` jusqu'à une décision de modération.
    """

    class Motif(models.TextChoices):
        SPAM = "spam", "Publicité ou spam"
        INJURIEUX = "injurieux", "Propos injurieux ou haineux"
        HORS_SUJET = "hors_sujet", "Hors sujet"
        FAUX_AVIS = "faux_avis", "Avis non authentique"
        DONNEES_PERSONNELLES = "donnees_personnelles", "Données personnelles"
        AUTRE = "autre", "Autre"

    class Statut(models.TextChoices):
        OUVERT = "ouvert", "Ouvert"
        ACCEPTE = "accepte", "Accepté (avis masqué)"
        REJETE = "rejete", "Rejeté"

    avis = models.ForeignKey(Avis, on_delete=models.PROTECT, related_name="signalements")
    auteur = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="signalements_avis")
    motif = models.CharField(max_length=25, choices=Motif.choices)
    commentaire = models.TextField(max_length=500, blank=True)
    statut = models.CharField(max_length=10, choices=Statut.choices, default=Statut.OUVERT)
    decision = models.ForeignKey(
        DecisionModeration, null=True, blank=True, on_delete=models.PROTECT, related_name="signalements_traites",
    )
    date_creation = models.DateTimeField(auto_now_add=True)
    date_traitement = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Signalement d'avis"
        verbose_name_plural = "Signalements d'avis"
        ordering = ["date_creation", "id"]
        constraints = [
            models.UniqueConstraint(fields=["avis", "auteur"], name="signalement_unique_par_avis_et_auteur"),
            models.CheckConstraint(
                condition=(
                    Q(statut="ouvert", decision__isnull=True, date_traitement__isnull=True)
                    | (~Q(statut="ouvert") & Q(decision__isnull=False, date_traitement__isnull=False))
                ),
                name="signalement_traitement_coherent",
            ),
        ]
        indexes = [
            # File d'attente des modérateurs : signalements ouverts, plus anciens d'abord.
            models.Index(fields=["statut", "date_creation"], name="signalement_statut_date"),
        ]

    def __str__(self):
        return f"Signalement {self.get_motif_display()} — avis {self.avis_id}"
