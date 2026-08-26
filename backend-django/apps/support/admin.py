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
    readonly_fields = ("id", "ticket_number", "created_at", "updated_at")
    inlines = [TicketMessageInline]


@admin.register(TicketMessage)
class TicketMessageAdmin(admin.ModelAdmin):
    list_display = ("ticket_link", "author", "author_role", "is_internal_note", "read_at", "created_at")
    list_filter = ("author_role", "is_internal_note")
    search_fields = ("ticket_link__ticket_number", "author__email", "content")
    readonly_fields = ("id", "created_at")


@admin.register(TicketAttachment)
class TicketAttachmentAdmin(admin.ModelAdmin):
    list_display = ("original_filename", "message", "file_type", "file_size", "created_at")
    list_filter = ("file_type",)
    search_fields = ("original_filename", "message__ticket_link__ticket_number")
    readonly_fields = ("id", "created_at")