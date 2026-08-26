from django.db import models
from django.shortcuts import get_object_or_404
from rest_framework.permissions import IsAuthenticated
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied

from .models import SupportTicket, TicketMessage, TicketAttachment
from .serializers import SupportTicketSerializer, TicketMessageSerializer, TicketAttachmentSerializer
from apps.utilisateurs.models import Role


STAFF_ROLES = [Role.ADMIN, Role.SUPER_ADMIN, Role.SUPPORT]


def get_visible_tickets(user):
    """Point unique de la règle de visibilité par rôle.
    Réutilisé partout où on doit vérifier l'accès à un ticket,
    pour éviter que la règle diverge entre les vues."""
    if user.role in [Role.ADMIN, Role.SUPER_ADMIN]:
        return SupportTicket.objects.all()
    if user.role == Role.VENDEUR:
        return SupportTicket.objects.filter(vendor__proprietaire=user)
    if user.role == Role.SUPPORT:
        return SupportTicket.objects.filter(
            models.Q(assigned_to=user) | models.Q(assigned_to__isnull=True)
        )
    return SupportTicket.objects.filter(created_by=user)


# ---------- SupportTicket ----------

class SupportTicketListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SupportTicketSerializer

    def get_queryset(self):
        return get_visible_tickets(self.request.user)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class SupportRetrieveUpdateDestroyView(generics.RetrieveUpdateDestroyAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SupportTicketSerializer

    def get_queryset(self):
        return get_visible_tickets(self.request.user)

    def perform_destroy(self, instance):
        """Empêche la suppression physique d'un ticket par un non-staff.

        Un ticket = trace/historique potentiellement utile en cas de litige.
        Un client qui veut "en finir" avec son ticket doit le FERMER
        (status=CLOSED via SupportTicketChangeStatusView), pas le supprimer.
        Seul le staff (admin/super_admin/support) peut réellement l'effacer.
        """
        if self.request.user.role not in STAFF_ROLES:
            raise PermissionDenied("Seul le staff peut supprimer un ticket.")
        instance.delete()


class SupportTicketChangeStatusView(APIView):
    """Changement de statut, réservé au staff (admin/super_admin/support),
    sauf pour le créateur qui peut fermer son propre ticket."""
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        user = request.user
        ticket = get_object_or_404(get_visible_tickets(user), pk=pk)
        new_status = request.data.get("status")

        is_staff = user.role in STAFF_ROLES
        is_owner_closing = (
            ticket.created_by == user
            and new_status == SupportTicket.Status.CLOSED
        )

        if not (is_staff or is_owner_closing):
            return Response({"detail": "Permission refusée."}, status=status.HTTP_403_FORBIDDEN)

        valid_statuses = [choice[0] for choice in SupportTicket.Status.choices]
        if new_status not in valid_statuses:
            return Response(
                {"detail": f"Statut invalide. Valeurs possibles : {valid_statuses}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        ticket.status = new_status
        ticket.save(update_fields=["status", "updated_at"])
        return Response(SupportTicketSerializer(ticket).data, status=status.HTTP_200_OK)


class SupportTicketRateView(APIView):
    """Permet au créateur du ticket de laisser une note de satisfaction,
    une seule fois. Impossible de la modifier une fois donnée."""
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        ticket = get_object_or_404(get_visible_tickets(request.user), pk=pk)

        if ticket.created_by != request.user:
            return Response(
                {"detail": "Seul le créateur du ticket peut le noter."},
                status=status.HTTP_403_FORBIDDEN
            )

        if ticket.satisfaction_rating is not None:
            return Response(
                {"detail": "Ce ticket a déjà été noté."},
                status=status.HTTP_400_BAD_REQUEST
            )

        rating = request.data.get("satisfaction_rating")

        try:
            rating = int(rating)
        except (TypeError, ValueError):
            return Response(
                {"detail": "La note doit être un nombre entier."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if rating < 1 or rating > 5:
            return Response(
                {"detail": "La note doit être comprise entre 1 et 5."},
                status=status.HTTP_400_BAD_REQUEST
            )

        ticket.satisfaction_rating = rating
        ticket.save(update_fields=["satisfaction_rating", "updated_at"])
        return Response(SupportTicketSerializer(ticket).data, status=status.HTTP_200_OK)


# ---------- TicketMessage ----------

class TicketMessageListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TicketMessageSerializer

    def _get_accessible_ticket(self):
        """Vérifie l'accès au ticket parent, 404 propre sinon.
        Utilisée par get_queryset ET perform_create pour garantir
        que la même règle s'applique à la lecture ET à l'écriture."""
        return get_object_or_404(
            get_visible_tickets(self.request.user),
            pk=self.kwargs["ticket_id"]
        )

    def get_queryset(self):
        ticket = self._get_accessible_ticket()
        queryset = TicketMessage.objects.filter(ticket_link=ticket)

        if self.request.user.role not in STAFF_ROLES:
            queryset = queryset.filter(is_internal_note=False)

        return queryset

    def perform_create(self, serializer):
        user = self.request.user
        ticket = self._get_accessible_ticket()  # revérifie l'accès avant d'écrire

        author_role = TicketMessage.AuthorRole.CLIENT
        if user.role == Role.VENDEUR:
            author_role = TicketMessage.AuthorRole.VENDOR
        elif user.role in STAFF_ROLES:
            author_role = TicketMessage.AuthorRole.SUPPORT

        serializer.save(ticket_link=ticket, author=user, author_role=author_role)


# ---------- TicketAttachment ----------

class TicketAttachmentListCreateView(generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TicketAttachmentSerializer

    def _get_accessible_message(self):
        """Vérifie que l'utilisateur a accès au ticket parent du message,
        pas seulement qu'il est connecté."""
        message = get_object_or_404(TicketMessage, pk=self.kwargs["message_id"])
        get_object_or_404(get_visible_tickets(self.request.user), pk=message.ticket_link_id)
        return message

    def get_queryset(self):
        message = self._get_accessible_message()
        return TicketAttachment.objects.filter(message=message)

    def perform_create(self, serializer):
        message = self._get_accessible_message()
        file_obj = self.request.FILES.get("file")

        serializer.save(
            message=message,
            original_filename=file_obj.name if file_obj else "",
            file_size=file_obj.size if file_obj else 0,
        )