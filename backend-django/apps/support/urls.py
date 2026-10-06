from django.urls import path
from .views import (
    SupportTicketListCreateView,
    SupportRetrieveUpdateView,
    SupportTicketChangeStatusView,
    SupportTicketAssignView,
    TicketMessageListCreateView,
    TicketAttachmentListCreateView,
    TicketAttachmentDownloadView,
    SupportTicketRateView
)

app_name = "support"

urlpatterns = [
    path("tickets/", SupportTicketListCreateView.as_view(), name="ticket-list-create"),
    path("tickets/<uuid:pk>/", SupportRetrieveUpdateView.as_view(), name="ticket-detail"),
    path("tickets/<uuid:pk>/status/", SupportTicketChangeStatusView.as_view(), name="ticket-change-status"),
    path("tickets/<uuid:pk>/assign/", SupportTicketAssignView.as_view(), name="ticket-assign"),

    path("tickets/<uuid:ticket_id>/messages/", TicketMessageListCreateView.as_view(), name="ticket-messages"),
    path("messages/<uuid:message_id>/attachments/", TicketAttachmentListCreateView.as_view(), name="message-attachments"),
    path("attachments/<uuid:pk>/", TicketAttachmentDownloadView.as_view(), name="attachment-download"),
    path("tickets/<uuid:pk>/rate/", SupportTicketRateView.as_view(), name="ticket-rate"),
]
