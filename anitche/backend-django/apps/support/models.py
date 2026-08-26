import uuid
from django.db import models
from django.conf import settings
from django.utils import timezone


class SupportTicket(models.Model):
    """Ticket de support ouvert par un client, vendeur ou courier.

    Le ticket peut être lié à une commande, une boutique ou un produit
    selon le motif (category), mais reste vivant même si l'objet référencé
    est supprimé (on_delete=SET_NULL) pour préserver l'historique support.
    """

    class Status(models.TextChoices):
        """Cycle de vie du ticket, du dépôt à la clôture."""
        OPEN = "open", "Ouvert"
        IN_PROGRESS = "in_progress", "En cours"
        WAITING_CUSTOMER = "waiting_customer", "En attente client"
        RESOLVED = "resolved", "Résolu"
        CLOSED = "closed", "Fermé"

    class Priority(models.TextChoices):
        """Niveau d'urgence, utilisé pour trier/filtrer la file support."""
        LOW = "low", "Basse"
        NORMAL = "normal", "Normale"
        HIGH = "high", "Haute"
        URGENT = "urgent", "Urgente"

    class Category(models.TextChoices):
        """Motif du ticket, sert aussi à orienter vers le bon service/agent."""
        DELIVERY = "delivery", "Livraison"
        PAYMENT = "payment", "Paiement"
        PRODUCT = "product", "Produit défectueux"
        VENDOR_DISPUTE = "vendor_dispute", "Litige vendeur"
        ACCOUNT = "account", "Compte"
        OTHER = "other", "Autre"

    # UUID plutôt qu'auto-increment : évite l'énumération d'ID dans les URLs/API
    # et reste cohérent avec le reste du projet (Boutique, etc.)
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Numéro lisible communiqué au client/support (ex: TCK-2026-000042),
    # généré séparément de l'id technique (non éditable, calculé côté save()).
    ticket_number = models.CharField(max_length=20, unique=True, editable=False)

    # CASCADE : un ticket n'a pas de sens sans son créateur, il disparaît avec lui.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="support_tickets",
    )

    # SET_NULL : si l'agent assigné est supprimé, le ticket reste, juste "non assigné".
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_support_tickets",  # évite le conflit avec created_by
    )

    # Boutique concernée par le litige (pas l'utilisateur vendeur directement) :
    # un litige vise l'activité commerciale visible, pas le compte en tant que tel.
    vendor = models.ForeignKey(
        "vendeurs.Boutique",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="support_tickets",
    )

   
    order = models.ForeignKey("commandes.Commande", on_delete=models.SET_NULL, null=True, blank=True, related_name="support_tickets")
    product = models.ForeignKey("catalogue.Produit", on_delete=models.SET_NULL, null=True, blank=True, related_name="support_tickets")

    subject = models.CharField(max_length=255)
    description = models.TextField()
    category = models.CharField(max_length=20, choices=Category.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL)

    # Note de satisfaction laissée par le client après résolution (ex: 1 à 5).
    satisfaction_rating = models.PositiveSmallIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]  # tickets les plus récents en premier

    def save(self, *args, **kwargs):
        if not self.ticket_number:
            self.ticket_number = f"TCK-{timezone.now().year}-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.ticket_number} - {self.subject}"


class TicketMessage(models.Model):
    """Message échangé dans le fil de discussion d'un ticket.

    author_role est dupliqué depuis le rôle de l'auteur au moment de l'écriture,
    pour garder une trace fidèle même si son rôle change plus tard (ex: un
    client qui devient vendeur ne doit pas réécrire l'historique du message).
    """

    class AuthorRole(models.TextChoices):
        """Qui parle dans ce message, pour l'affichage frontend (bulle, badge)."""
        CLIENT = "client", "Client"
        VENDOR = "vendor", "Vendeur"
        SUPPORT = "support", "Support ANITCHE"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # CASCADE : les messages n'ont pas de raison d'exister sans leur ticket parent.
    ticket_link = models.ForeignKey(
        "support.SupportTicket",
        on_delete=models.CASCADE,
        related_name="messages",
    )

    # SET_NULL : si l'auteur supprime son compte, le message reste (avec
    # author_role qui garde la trace du type d'auteur, même sans identité précise).
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ticket_messages",
    )
    author_role = models.CharField(
        max_length=10,
        choices=AuthorRole.choices,
    )

    content = models.TextField()

    # Note privée entre agents support/admin, jamais exposée côté client
    # (à filtrer explicitement dans les serializers/vues côté API client).
    is_internal_note = models.BooleanField(
        default=False,
        help_text="Note interne visible uniquement par le support/admin, invisible au client.",
    )

    # None = pas encore lu ; sinon date/heure exacte de lecture (style "Lu à 14h32").
    read_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]  # ordre chronologique, pour lire la conversation dans l'ordre

    def __str__(self):
        # ticket_link, pas ticket : c'est le nom réel du champ défini plus haut.
        return f"Message de {self.author_role} sur {self.ticket_link.ticket_number}"


class TicketAttachment(models.Model):
    """Pièce jointe rattachée à un message de ticket (photo, capture, PDF)."""

    class FileType(models.TextChoices):
        """Type de fichier, utilisé côté frontend pour choisir preview vs icône."""
        IMAGE = "image", "Image"
        DOCUMENT = "document", "Document"
        OTHER = "other", "Autre"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # CASCADE : une pièce jointe n'a pas de sens sans le message qui la porte.
    message = models.ForeignKey(
        "support.TicketMessage",
        on_delete=models.CASCADE,
        related_name="attachments",
    )

    # Rangement par année/mois pour éviter d'entasser tous les fichiers dans un seul dossier.
    file = models.FileField(upload_to="support/attachments/%Y/%m/")
    file_type = models.CharField(max_length=10, choices=FileType.choices, default=FileType.OTHER)

    # Nom original conservé séparément : le nom stocké sur disque/serveur
    # peut être renommé (hashé, horodaté) pour éviter les collisions.
    original_filename = models.CharField(max_length=255)
    file_size = models.PositiveIntegerField(help_text="Taille en octets")

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.original_filename


