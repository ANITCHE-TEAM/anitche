import uuid
from datetime import timedelta

from django.db import models
from django.db.models import Q
from django.db.models.functions import Floor
from django.conf import settings
from django.utils import timezone

from apps.core.storage import CheminUploadUUID
from apps.core.validators import validateur_image_standard


class DemandeRetour(models.Model):
    """Demande de retour et de remboursement initiée par un client après la
    livraison. Le statut ne change que par apps.retours.services."""

    class Motif(models.TextChoices):
        PRODUIT_DEFECTUEUX = "produit_defectueux", "Produit défectueux ou endommagé"
        NON_CONFORME = "non_conforme", "Article non conforme à la description"
        MAUVAISE_TAILLE = "mauvaise_taille", "Taille ou variante incorrecte"
        ARTICLE_MANQUANT = "article_manquant", "Article manquant dans le colis"
        CHANGEMENT_AVIS = "changement_avis", "Changement d'avis (droit de rétractation)"
        AUTRE = "autre", "Autre motif"

    class Statut(models.TextChoices):
        DEMANDE = "demande", "Demande soumise"
        APPROUVE = "approuve", "Approuvée"
        REJETE = "rejete", "Rejetée"
        EN_TRANSIT = "en_transit", "Colis retour en transit"
        RECEPTIONNE = "receptionne", "Colis réceptionné"
        REMBOURSE = "rembourse", "Remboursé"
        CLOTURE = "cloture", "Clôturé"
        ANNULE = "annule", "Annulée par le client"
        REFUSE_VENDEUR = "refuse_vendeur", "Refusée par le vendeur (contestable)"
        EN_MODERATION = "en_moderation", "En examen par la modération"
        REFUSE_MODERATION = "refuse_moderation", "Refusée par la modération (contestable)"
        EN_ARBITRAGE = "en_arbitrage", "En arbitrage par l'administration"

    class TypeResolution(models.TextChoices):
        REMBOURSEMENT = "remboursement", "Remboursement"
        ECHANGE = "echange", "Échange standard"
        AVOIR = "avoir", "Avoir boutique"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    numero_retour = models.CharField(max_length=30, unique=True, editable=False, db_index=True)

    commande = models.ForeignKey(
        "commandes.Commande",
        on_delete=models.CASCADE,
        related_name="retours",
        verbose_name="Commande d'origine",
    )

    client = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="demandes_retour",
        verbose_name="Client",
    )

    boutique = models.ForeignKey(
        "vendeurs.Boutique",
        on_delete=models.CASCADE,
        related_name="demandes_retour",
        verbose_name="Boutique concernée",
    )

    motif = models.CharField(max_length=30, choices=Motif.choices, default=Motif.PRODUIT_DEFECTUEUX)
    type_resolution = models.CharField(max_length=20, choices=TypeResolution.choices, default=TypeResolution.REMBOURSEMENT)
    statut = models.CharField(max_length=20, choices=Statut.choices, default=Statut.DEMANDE, db_index=True)

    description = models.TextField(help_text="Explications fournies par le client sur le problème rencontré")
    montant_remboursement = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text="Montant total à rembourser calculé sur la base des articles retournés",
    )
    # Part des frais de livraison comprise dans montant_remboursement : la
    # totalité des frais payés, une fois par commande, pour un motif
    # imputable au vendeur (apps.retours.services.MOTIFS_FRAIS_REMBOURSES).
    # Supportée par le vendeur (AjustementVendeur au remboursement).
    frais_livraison_rembourses = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    reponse_vendeur = models.TextField(blank=True, help_text="Commentaire ou motif de décision fourni par le vendeur/staff")

    date_creation = models.DateTimeField(auto_now_add=True)
    date_traitement = models.DateTimeField(null=True, blank=True)
    date_cloture = models.DateTimeField(null=True, blank=True)
    date_mise_a_jour = models.DateTimeField(auto_now=True)

    # Échéances du circuit de décision (apps.retours.services) : réponse du
    # vendeur (statut demande), contestation d'un refus (statuts refuse_*).
    date_limite_vendeur = models.DateTimeField(null=True, blank=True)
    date_limite_contestation = models.DateTimeField(null=True, blank=True)
    # Entrée en modération (escalade ou contestation) : la demande devient
    # visible des modérateurs. Entrée en arbitrage par l'administration.
    date_moderation = models.DateTimeField(null=True, blank=True)
    date_arbitrage = models.DateTimeField(null=True, blank=True)
    # Rappel ou alerte envoyé pour le statut actuel (une seule fois par
    # statut) ; remis à vide à chaque changement de statut.
    date_relance = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Demande de retour"
        verbose_name_plural = "Demandes de retour"
        ordering = ["-date_creation"]
        constraints = [
            # Sans échéance, une demande sans réponse ou un refus non contesté
            # bloquerait le retour et le reversement du vendeur sans fin.
            models.CheckConstraint(
                condition=~Q(statut="demande") | Q(date_limite_vendeur__isnull=False),
                name="retour_demande_avec_echeance_vendeur",
            ),
            models.CheckConstraint(
                condition=~Q(statut__in=("refuse_vendeur", "refuse_moderation"))
                | Q(date_limite_contestation__isnull=False),
                name="retour_refus_avec_echeance_contestation",
            ),
            # Montant rendu au client : un nombre entier de FCFA, jamais négatif.
            models.CheckConstraint(
                condition=Q(montant_remboursement__isnull=True)
                | (Q(montant_remboursement__gte=0) & Q(montant_remboursement=Floor("montant_remboursement"))),
                name="retour_montant_entier_positif",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self.numero_retour:
            annee = timezone.now().year
            self.numero_retour = f"RET-{annee}-{uuid.uuid4().hex[:8].upper()}"
        if self._state.adding and self.statut == self.Statut.DEMANDE and self.date_limite_vendeur is None:
            # Le vendeur a RETOUR_DELAI_REPONSE_VENDEUR_HEURES pour répondre
            # à partir de la création.
            self.date_limite_vendeur = timezone.now() + timedelta(hours=settings.RETOUR_DELAI_REPONSE_VENDEUR_HEURES)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.numero_retour} — Commande {self.commande.numero_commande} ({self.get_statut_display()})"


class RetourItem(models.Model):
    """Article spécifique d'une commande inclus dans la demande de retour."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    demande_retour = models.ForeignKey(
        DemandeRetour,
        on_delete=models.CASCADE,
        related_name="articles",
        verbose_name="Demande de retour parente",
    )

    commande_item = models.ForeignKey(
        "commandes.CommandeItem",
        on_delete=models.CASCADE,
        related_name="retours",
        verbose_name="Ligne de commande retournée",
    )

    quantite = models.PositiveIntegerField(default=1)

    class Meta:
        verbose_name = "Article retourné"
        verbose_name_plural = "Articles retournés"

    def __str__(self):
        return f"{self.quantite}x {self.commande_item.nom_produit} (Demande {self.demande_retour.numero_retour})"


class PhotoRetour(models.Model):
    """Photo justificative attachée à une demande de retour."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    demande_retour = models.ForeignKey(
        DemandeRetour,
        on_delete=models.CASCADE,
        related_name="photos",
        verbose_name="Demande de retour",
    )

    # Nom de fichier en UUID : le nom d'origine (souvent le nom du client,
    # ou d'une pièce d'identité photographiée par erreur) n'est jamais conservé.
    image = models.ImageField(
        upload_to=CheminUploadUUID("retours/preuves"),
        validators=[validateur_image_standard],
    )
    date_ajout = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Photo justificative de retour"
        verbose_name_plural = "Photos justificatives de retour"
        ordering = ["date_ajout"]

    def __str__(self):
        return f"Photo de preuve ({self.demande_retour.numero_retour})"


class RetourHistorique(models.Model):
    """Trace chaque étape d'une demande de retour : qui, quand, depuis quel
    statut, vers quel statut, pourquoi.

    Écrit uniquement par apps.retours.services, dans la transaction du
    changement. Lecture seule : jamais modifié ni supprimé (sauf avec la
    demande elle-même). La première ligne (création) n'a pas d'ancien
    statut ; les demandes antérieures à l'historique n'ont de lignes qu'à
    partir de leur changement de statut suivant.
    """

    class Source(models.TextChoices):
        """Au titre de quoi l'étape a été faite, seule information d'acteur
        montrée aux parties (un modérateur n'est jamais nommé)."""
        CLIENT = "client", "Client"
        VENDEUR = "vendeur", "Vendeur"
        MODERATION = "moderation", "Modération"
        ADMINISTRATION = "administration", "Administration"
        SYSTEME = "systeme", "Système"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    demande_retour = models.ForeignKey(DemandeRetour, on_delete=models.CASCADE, related_name="historique")
    ancien_statut = models.CharField(max_length=20, choices=DemandeRetour.Statut.choices, blank=True)
    nouveau_statut = models.CharField(max_length=20, choices=DemandeRetour.Statut.choices)
    action = models.CharField(max_length=32)
    source = models.CharField(max_length=20, choices=Source.choices)
    # Compte à l'origine de l'étape ; vide pour le système. Réservé à
    # l'administration : les parties ne voient que `source`.
    auteur = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="etapes_retour",
    )
    # Motif du refus, de la contestation ou de la décision : texte libre,
    # lu par l'autre partie et par la modération.
    commentaire = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        verbose_name = "historique de retour"
        verbose_name_plural = "historique des retours"

    def __str__(self):
        return f"{self.demande_retour_id} : {self.ancien_statut or '∅'} → {self.nouveau_statut} ({self.action})"
