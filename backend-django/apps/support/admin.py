from django.contrib import admin
from .models import SupportTicket, TicketMessage, TicketAttachment


class TicketMessageInline(admin.TabularInline):
    model = TicketMessage
    extra = 0
    readonly_fields = ("author", "author_role", "content", "is_internal_note", "read_at", "created_at")
    can_delete = False


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = (
        "ticket_number", "subject", "created_by", "category",
        "status", "priority", "assigned_to", "created_at",
    )
    list_filter = ("status", "priority", "category")
    search_fields = ("ticket_number", "subject", "created_by__email", "assigned_to__email")
    # 'satisfaction_rating' verrouillé : l'API impose explicitement qu'un
    # ticket ne soit noté qu'une seule fois par son créateur
    # (SupportTicketRateView refuse toute seconde notation) — laisser ce
    # champ modifiable dans l'admin permettrait de fabriquer ou de
    # modifier une note de satisfaction après coup, faussant tout
    # reporting basé dessus.
    readonly_fields = ("id", "ticket_number", "created_at", "updated_at", "satisfaction_rating")
    inlines = [TicketMessageInline]


@admin.register(TicketMessage)
class TicketMessageAdmin(admin.ModelAdmin):
    list_display = ("ticket_link", "author", "author_role", "is_internal_note", "read_at", "created_at")
    list_filter = ("author_role", "is_internal_note")
    search_fields = ("ticket_link__ticket_number", "author__email", "content")
    # Un message de ticket est une pièce d'échange entre client/vendeur/
    # support : le laisser éditable dans l'admin permettrait de réécrire
    # ce qu'un client a réellement dit, de réattribuer un message à un
    # autre auteur (author/author_role), ou de basculer
    # is_internal_note après coup — masquant une preuve au client (note
    # rendue interne) ou exposant une remarque interne confidentielle
    # (note rendue visible). Tout est verrouillé, y compris le lien vers
    # le ticket parent.
    readonly_fields = (
        "id", "ticket_link", "author", "author_role", "content",
        "is_internal_note", "read_at", "created_at",
    )


@admin.register(TicketAttachment)
class TicketAttachmentAdmin(admin.ModelAdmin):
    list_display = ("original_filename", "message", "file_type", "file_size", "created_at")
    list_filter = ("file_type",)
    search_fields = ("original_filename", "message__ticket_link__ticket_number")
    # Même principe que PhotoRetour (apps.retours.admin) : une pièce
    # jointe peut être une preuve dans un litige — son remplacement après
    # coup ouvrirait une possibilité de falsification.
    readonly_fields = (
        "id", "message", "file", "file_type",
        "original_filename", "file_size", "created_at",
    )