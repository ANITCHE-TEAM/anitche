"""Support ticket rules (docs/MODULE_SUPPORT.md).

Status flow (staff; the creator may only close his own ticket):

    open ─► in_progress ⇄ waiting_customer ─► resolved ─► closed (final)
      └──────────┴──────────────┴─────────────────┴──────► closed

A customer reply on a ticket waiting for him (or resolved) puts it back
in progress. SUPPORT agents work on the queue of unassigned tickets: the
first agent who acts on one takes it; admins see and reassign everything.
"""

import logging
import os

from django.db import models, transaction

from apps.notifications.models import Notification
from apps.notifications import liens
from apps.notifications.services import ServiceNotification
from apps.utilisateurs.models import Role, Utilisateur
from apps.vendeurs.permissions import ROLES_ADMINISTRATION

from .models import SupportTicket, TicketAttachment, TicketMessage

logger = logging.getLogger(__name__)

Status = SupportTicket.Status
STAFF_ROLES = (Role.ADMIN, Role.SUPER_ADMIN, Role.SUPPORT)
ATTACHMENTS_PER_MESSAGE = 5
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")

ALLOWED_TRANSITIONS = {
    Status.OPEN: {Status.IN_PROGRESS, Status.WAITING_CUSTOMER, Status.RESOLVED, Status.CLOSED},
    Status.IN_PROGRESS: {Status.WAITING_CUSTOMER, Status.RESOLVED, Status.CLOSED},
    Status.WAITING_CUSTOMER: {Status.IN_PROGRESS, Status.RESOLVED, Status.CLOSED},
    Status.RESOLVED: {Status.IN_PROGRESS, Status.CLOSED},
    Status.CLOSED: set(),
}
#: Categories where the shop concerned sees the ticket and may answer.
VENDOR_CATEGORIES = (SupportTicket.Category.PRODUCT, SupportTicket.Category.VENDOR_DISPUTE)


class SupportError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def is_staff_member(user):
    return user.role in STAFF_ROLES


def get_visible_tickets(user):
    """Single visibility rule (read AND write access start from here).

    Admin: everything. SUPPORT agent: his tickets and the unassigned queue.
    Vendor: tickets he opened and those about his shop. Others: their own.
    """
    if user.role in ROLES_ADMINISTRATION:
        return SupportTicket.objects.all()
    if user.role == Role.SUPPORT:
        return SupportTicket.objects.filter(models.Q(assigned_to=user) | models.Q(assigned_to__isnull=True))
    if user.role == Role.VENDEUR:
        return SupportTicket.objects.filter(models.Q(vendor__proprietaire=user) | models.Q(created_by=user))
    return SupportTicket.objects.filter(created_by=user)


def lock_visible_ticket(user, ticket_id):
    ticket = get_visible_tickets(user).select_for_update(of=("self",)).filter(pk=ticket_id).first()
    if ticket is None:
        raise SupportError("Ticket introuvable.", 404)
    return ticket


def take_if_unassigned(ticket, user):
    """A SUPPORT agent acting on a queued ticket takes it (under lock)."""
    if user.role == Role.SUPPORT and ticket.assigned_to_id is None:
        ticket.assigned_to = user
        ticket.save(update_fields=["assigned_to", "updated_at"])


# =====================================================================
# STATUS AND ASSIGNMENT
# =====================================================================

def change_status(ticket_id, user, new_status):
    if new_status not in Status.values:
        raise SupportError(f"Statut invalide. Valeurs possibles : {list(Status.values)}")
    with transaction.atomic():
        ticket = lock_visible_ticket(user, ticket_id)
        staff = is_staff_member(user)
        if not staff and not (ticket.created_by_id == user.pk and new_status == Status.CLOSED):
            raise SupportError("Permission refusée.", 403)
        if new_status not in ALLOWED_TRANSITIONS[ticket.status]:
            raise SupportError(f"Transition invalide : impossible de passer de '{ticket.status}' à '{new_status}'.")
        if staff:
            take_if_unassigned(ticket, user)
        ticket.status = new_status
        ticket.save(update_fields=["status", "updated_at"])
        if staff and ticket.created_by_id != user.pk:
            _notify_customer(ticket, f"Votre ticket {ticket.ticket_number} est désormais : {ticket.get_status_display()}.")
    return ticket


def assign(ticket_id, user, assignee_id=None):
    """SUPPORT agent: takes a queued ticket for himself. Admin: assigns to
    any active SUPPORT/admin account (or to himself without assignee)."""
    if not is_staff_member(user):
        raise SupportError("Permission refusée.", 403)
    with transaction.atomic():
        ticket = lock_visible_ticket(user, ticket_id)
        if ticket.status == Status.CLOSED:
            raise SupportError("Ticket fermé.")
        if user.role == Role.SUPPORT:
            if assignee_id not in (None, "", user.pk, str(user.pk)):
                raise SupportError("Seule l'administration réassigne un ticket.", 403)
            if ticket.assigned_to_id not in (None, user.pk):
                raise SupportError("Ticket introuvable.", 404)
            assignee = user
        else:
            assignee = user if assignee_id in (None, "") else Utilisateur.objects.filter(
                pk=assignee_id, role__in=STAFF_ROLES, is_active=True,
            ).first()
            if assignee is None:
                raise SupportError("Agent introuvable (rôle support ou administration, compte actif).")
        ticket.assigned_to = assignee
        ticket.save(update_fields=["assigned_to", "updated_at"])
    logger.info("Ticket %s assigné à user_id=%s par user_id=%s", ticket.ticket_number, assignee.pk, user.pk)
    return ticket


# =====================================================================
# MESSAGES AND ATTACHMENTS
# =====================================================================

def author_role_for(user):
    if is_staff_member(user):
        return TicketMessage.AuthorRole.SUPPORT
    if user.role == Role.VENDEUR:
        return TicketMessage.AuthorRole.VENDOR
    return TicketMessage.AuthorRole.CLIENT


def add_message(ticket_id, user, content, is_internal_note=False):
    with transaction.atomic():
        ticket = lock_visible_ticket(user, ticket_id)
        if ticket.status == Status.CLOSED:
            raise SupportError("Ticket fermé : ouvrez un nouveau ticket.")
        staff = is_staff_member(user)
        internal = bool(is_internal_note) and staff  # only staff writes internal notes
        if staff:
            take_if_unassigned(ticket, user)
        message = TicketMessage.objects.create(
            ticket_link=ticket, author=user, author_role=author_role_for(user),
            content=content, is_internal_note=internal,
        )
        if internal:
            return message
        if staff:
            if ticket.status == Status.OPEN:
                ticket.status = Status.IN_PROGRESS
                ticket.save(update_fields=["status", "updated_at"])
            if ticket.created_by_id != user.pk:
                _notify_customer(ticket, f"Nouvelle réponse du support sur votre ticket {ticket.ticket_number}.")
        else:
            if ticket.status in (Status.WAITING_CUSTOMER, Status.RESOLVED):
                ticket.status = Status.IN_PROGRESS
                ticket.save(update_fields=["status", "updated_at"])
            _notify_staff(ticket, f"Nouveau message sur le ticket {ticket.ticket_number}.")
    return message


def visible_message(user, message_id):
    """Message of a visible ticket; internal notes only for staff."""
    message = TicketMessage.objects.select_related("ticket_link").filter(pk=message_id).first()
    if message is None or not get_visible_tickets(user).filter(pk=message.ticket_link_id).exists():
        raise SupportError("Message introuvable.", 404)
    if message.is_internal_note and not is_staff_member(user):
        raise SupportError("Message introuvable.", 404)
    return message


def check_new_attachment(user, message_id):
    """Only the author attaches files to his own message, while the ticket
    is not closed, within ATTACHMENTS_PER_MESSAGE (under lock)."""
    message = visible_message(user, message_id)
    message = TicketMessage.objects.select_for_update().select_related("ticket_link").get(pk=message.pk)
    if message.author_id != user.pk:
        raise SupportError("Seul l'auteur du message peut y joindre un fichier.", 403)
    if message.ticket_link.status == Status.CLOSED:
        raise SupportError("Ticket fermé.")
    if message.attachments.count() >= ATTACHMENTS_PER_MESSAGE:
        raise SupportError(f"{ATTACHMENTS_PER_MESSAGE} pièces jointes au plus par message.")
    return message


def file_type_for(name):
    extension = os.path.splitext(name or "")[1].lower()
    return TicketAttachment.FileType.IMAGE if extension in IMAGE_EXTENSIONS else TicketAttachment.FileType.DOCUMENT


def visible_attachment(user, attachment_id):
    attachment = TicketAttachment.objects.select_related("message").filter(pk=attachment_id).first()
    if attachment is None:
        raise SupportError("Pièce jointe introuvable.", 404)
    visible_message(user, attachment.message_id)
    return attachment


# =====================================================================
# NOTIFICATIONS
# =====================================================================

def notify_new_ticket(ticket):
    """New ticket: the support team (in-app, like administration alerts)."""
    agents = Utilisateur.objects.filter(role=Role.SUPPORT, is_active=True).only("pk")
    Notification.objects.bulk_create([
        Notification(destinataire=agent, titre=f"Nouveau ticket {ticket.ticket_number}",
                     message=f"{ticket.get_category_display()} : {ticket.subject[:150]}",
                     type_notification=Notification.TypeNotification.SUPPORT,
                     lien_redirection=liens.lien_ticket(ticket), metadata={"ticket_id": str(ticket.pk)})
        for agent in agents
    ])


def _notify_customer(ticket, message):
    ServiceNotification.notifier_utilisateur(
        ticket.created_by, titre=f"Ticket {ticket.ticket_number}", message=message,
        type_notification=Notification.TypeNotification.SUPPORT,
        lien_redirection=liens.lien_ticket(ticket), metadata={"ticket_id": str(ticket.pk)},
    )


def _notify_staff(ticket, message):
    """Assigned agent (in-app). A queued ticket is already in every agent's
    list, announced at creation: no new alert per message."""
    if ticket.assigned_to_id:
        ServiceNotification.notifier_utilisateur(
            ticket.assigned_to, titre=f"Ticket {ticket.ticket_number}", message=message,
            type_notification=Notification.TypeNotification.SUPPORT, email=False,
            lien_redirection=liens.lien_ticket(ticket), metadata={"ticket_id": str(ticket.pk)},
        )
