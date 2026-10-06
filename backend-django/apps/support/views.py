import logging

from django.db import transaction
from django.http import FileResponse, Http404
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.exceptions import ErreurMetier
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view, inline_serializer
from rest_framework import serializers

from apps.vendeurs.permissions import ROLES_ADMINISTRATION
from config.schema import FICHIER_DOCUMENT, erreurs

from . import services
from .models import SupportTicket, TicketAttachment, TicketMessage
from .serializers import SupportTicketSerializer, TicketAttachmentSerializer, TicketMessageSerializer
from .services import STAFF_ROLES, get_visible_tickets  # noqa: F401 (réexportés : point d'entrée historique)

logger_securite = logging.getLogger("securite")


def api_error(error):
    """SupportError → exception rendered in the common error format."""
    return ErreurMetier(error.message, error.status_code)


class ScopedOnPostMixin:
    """Dedicated rate limit on creation only; reads keep the general rate."""

    post_throttle_scope = None

    def get_throttles(self):
        self.throttle_scope = self.post_throttle_scope if self.request.method == "POST" else None
        return super().get_throttles()


# ---------- SupportTicket ----------

#: ?perimetre=mes_tickets : les tickets créés par le compte, quel que soit son
#: rôle (portail client d'un vendeur, sans les litiges de sa boutique).
PERIMETRE_MES_TICKETS = "mes_tickets"

PARAMETRE_PERIMETRE = OpenApiParameter(
    "perimetre", enum=[PERIMETRE_MES_TICKETS],
    description=(
        "`mes_tickets` : seulement les tickets créés par ce compte, quel que soit le rôle (le portail client "
        "l'envoie toujours). Valeur inconnue : ignorée."
    ),
)


def tickets_du_perimetre(request):
    """Tickets visibles, restreints à ceux créés par le compte si
    `?perimetre=mes_tickets` (ne restreint que l'ensemble déjà autorisé)."""
    queryset = get_visible_tickets(request.user)
    if request.query_params.get("perimetre") == PERIMETRE_MES_TICKETS:
        queryset = queryset.filter(created_by=request.user)
    return queryset


@extend_schema_view(
    get=extend_schema(
        summary="Tickets visibles par ce compte",
        parameters=[
            PARAMETRE_PERIMETRE,
            OpenApiParameter(
                "non_assigne", bool,
                description="Administration seulement : `1` ou `true` pour la file des tickets sans agent. Ignoré pour les autres rôles.",
            ),
            OpenApiParameter(
                "status", enum=[valeur for valeur, _ in SupportTicket.Status.choices],
                description="Administration seulement. Ignoré pour les autres rôles.",
            ),
        ],
    ),
    post=extend_schema(summary="Ouvrir un ticket"),
)
class SupportTicketListCreateView(ScopedOnPostMixin, generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = SupportTicketSerializer
    post_throttle_scope = "support_ticket"

    def get_queryset(self):
        queryset = tickets_du_perimetre(self.request)
        if self.request.user.role in ROLES_ADMINISTRATION:
            # Tableau de bord : file d'attente et tickets par statut (le
            # `count` de la liste filtrée sert de compteur).
            parametres = self.request.query_params
            if parametres.get("non_assigne") in ("1", "true"):
                queryset = queryset.filter(assigned_to__isnull=True)
            statut = parametres.get("status")
            if statut in SupportTicket.Status.values:
                queryset = queryset.filter(status=statut)
        return queryset

    def perform_create(self, serializer):
        order = serializer.validated_data.get("order")
        product = serializer.validated_data.get("product")
        category = serializer.validated_data.get("category")
        # La boutique ne voit le ticket (et n'y répond) que pour un litige
        # sur un produit ou avec elle, jamais pour une question de paiement
        # ou de compte du client.
        vendor = None
        if category in services.VENDOR_CATEGORIES:
            vendor = product.boutique if product is not None else (order.boutique if order is not None else None)
        with transaction.atomic():
            ticket = serializer.save(created_by=self.request.user, vendor=vendor)
            transaction.on_commit(lambda: services.notify_new_ticket(ticket))


@extend_schema_view(
    get=extend_schema(summary="Détail d'un ticket", parameters=[PARAMETRE_PERIMETRE]),
    put=extend_schema(
        summary="Reclasser un ticket (équipe support)",
        responses={200: SupportTicketSerializer, **erreurs(403)},
    ),
    patch=extend_schema(
        summary="Reclasser un ticket (équipe support)",
        description="Catégorie et priorité, réservées à l'équipe support et à l'administration (403 sinon).",
        responses={200: SupportTicketSerializer, **erreurs(403)},
    ),
)
class SupportRetrieveUpdateView(generics.RetrieveUpdateAPIView):
    """Détail et reclassement (category/priority, staff seulement). Aucune
    suppression par l'API : un ticket est un historique de litige."""

    permission_classes = [IsAuthenticated]
    serializer_class = SupportTicketSerializer

    def get_queryset(self):
        return tickets_du_perimetre(self.request)

    def perform_update(self, serializer):
        """La visibilité autorise à VOIR ce ticket, pas à le MODIFIER."""
        user = self.request.user
        if user.role not in STAFF_ROLES:
            raise PermissionDenied("Seul le staff peut modifier la catégorie ou la priorité d'un ticket.")
        with transaction.atomic():
            services.take_if_unassigned(serializer.instance, user)
            serializer.save()


@extend_schema(
    summary="Changer le statut d'un ticket",
    description="Équipe support ; le créateur peut seulement fermer son ticket. Transition invalide : 400.",
    request=inline_serializer("ChangementStatutTicketRequest", {
        "status": serializers.ChoiceField(choices=SupportTicket.Status.choices),
    }),
    responses={200: SupportTicketSerializer, **erreurs(403, 404)},
)
class SupportTicketChangeStatusView(APIView):
    """Transitions réservées au staff (table services.ALLOWED_TRANSITIONS),
    sauf pour le créateur qui peut fermer son propre ticket."""

    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        try:
            ticket = services.change_status(pk, request.user, request.data.get("status"))
        except services.SupportError as error:
            raise api_error(error)
        return Response(SupportTicketSerializer(ticket, context={"request": request}).data)


@extend_schema(
    summary="Prendre ou assigner un ticket",
    description="Agent support : prendre un ticket de la file (sans corps). Administration : assigner à un agent (`assigned_to`).",
    request=inline_serializer("AssignationTicketRequest", {
        "assigned_to": serializers.IntegerField(required=False, help_text="Identifiant de l'agent (administration)."),
    }),
    responses={200: SupportTicketSerializer, **erreurs(403, 404)},
)
class SupportTicketAssignView(APIView):
    """Agent support : prendre un ticket de la file. Administration :
    assigner à un agent (`assigned_to`) ou à soi."""

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            ticket = services.assign(pk, request.user, request.data.get("assigned_to"))
        except services.SupportError as error:
            raise api_error(error)
        return Response(SupportTicketSerializer(ticket, context={"request": request}).data)


@extend_schema(
    summary="Noter un ticket résolu ou fermé",
    description="Créateur du ticket, une seule fois (403 pour un autre compte, 400 si pas encore résolu ou déjà noté).",
    request=inline_serializer("NoteTicketRequest", {
        "satisfaction_rating": serializers.IntegerField(min_value=1, max_value=5),
    }),
    responses={200: SupportTicketSerializer, **erreurs(403)},
)
class SupportTicketRateView(APIView):
    """Le créateur note le ticket une fois, quand il est résolu ou fermé."""

    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        with transaction.atomic():
            ticket = get_visible_tickets(request.user).select_for_update(of=("self",)).filter(pk=pk).first()
            if ticket is None:
                raise Http404
            if ticket.created_by_id != request.user.pk:
                raise PermissionDenied("Seul le créateur du ticket peut le noter.")
            if ticket.status not in (SupportTicket.Status.RESOLVED, SupportTicket.Status.CLOSED):
                raise ErreurMetier("Le ticket se note une fois résolu ou fermé.")
            if ticket.satisfaction_rating is not None:
                raise ErreurMetier("Ce ticket a déjà été noté.")
            try:
                rating = int(request.data.get("satisfaction_rating"))
            except (TypeError, ValueError):
                raise ValidationError({"satisfaction_rating": ["La note doit être un nombre entier."]})
            if rating < 1 or rating > 5:
                raise ValidationError({"satisfaction_rating": ["La note doit être comprise entre 1 et 5."]})
            ticket.satisfaction_rating = rating
            ticket.save(update_fields=["satisfaction_rating", "updated_at"])
        return Response(SupportTicketSerializer(ticket, context={"request": request}).data)


# ---------- TicketMessage ----------

@extend_schema_view(
    get=extend_schema(summary="Messages d'un ticket", description="Les notes internes ne sont visibles que de l'équipe support."),
    post=extend_schema(
        summary="Écrire un message",
        description="Ticket fermé : 400.",
        responses={201: TicketMessageSerializer, **erreurs(403)},
    ),
)
class TicketMessageListCreateView(ScopedOnPostMixin, generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TicketMessageSerializer
    post_throttle_scope = "support_message"

    def _get_accessible_ticket(self):
        ticket = get_visible_tickets(self.request.user).filter(pk=self.kwargs["ticket_id"]).first()
        if ticket is None:
            raise Http404
        return ticket

    def get_queryset(self):
        ticket = self._get_accessible_ticket()
        queryset = TicketMessage.objects.filter(ticket_link=ticket)
        if self.request.user.role not in STAFF_ROLES:
            queryset = queryset.filter(is_internal_note=False)
        return queryset

    def list(self, request, *args, **kwargs):
        reponse = super().list(request, *args, **kwargs)
        # Messages des autres parties affichés : marqués lus (« Lu à … »).
        self.get_queryset().filter(read_at__isnull=True).exclude(author=request.user).update(read_at=timezone.now())
        return reponse

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            message = services.add_message(
                self.kwargs["ticket_id"], request.user,
                serializer.validated_data["content"], serializer.validated_data.get("is_internal_note", False),
            )
        except services.SupportError as error:
            raise api_error(error)
        return Response(self.get_serializer(message).data, status=status.HTTP_201_CREATED)


# ---------- TicketAttachment ----------

@extend_schema_view(
    get=extend_schema(summary="Pièces jointes d'un message"),
    post=extend_schema(
        summary="Joindre un fichier à un message",
        description="Auteur du message seulement (403 sinon), 5 fichiers au plus, ticket non fermé.",
        request={"multipart/form-data": TicketAttachmentSerializer},
        responses={201: TicketAttachmentSerializer, **erreurs(403)},
    ),
)
class TicketAttachmentListCreateView(ScopedOnPostMixin, generics.ListCreateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = TicketAttachmentSerializer
    post_throttle_scope = "support_piece_jointe"

    def get_queryset(self):
        try:
            message = services.visible_message(self.request.user, self.kwargs["message_id"])
        except services.SupportError:
            raise Http404
        return TicketAttachment.objects.filter(message=message).order_by("created_at")

    def create(self, request, *args, **kwargs):
        file_obj = request.FILES.get("file")
        with transaction.atomic():
            try:
                message = services.check_new_attachment(request.user, self.kwargs["message_id"])
            except services.SupportError as error:
                raise api_error(error)
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            attachment = serializer.save(
                message=message,
                original_filename=file_obj.name[:255] if file_obj else "",
                file_size=file_obj.size if file_obj else 0,
                file_type=services.file_type_for(file_obj.name if file_obj else ""),
            )
        return Response(self.get_serializer(attachment).data, status=status.HTTP_201_CREATED)


@extend_schema(
    summary="Télécharger une pièce jointe",
    responses=FICHIER_DOCUMENT,
)
class TicketAttachmentDownloadView(APIView):
    """Fichier servi par Django après contrôle d'accès (mêmes règles que le
    message) : /media/ n'est pas exposé en production."""

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            attachment = services.visible_attachment(request.user, pk)
        except services.SupportError:
            raise Http404
        try:
            contenu = attachment.file.open("rb")
        except FileNotFoundError:
            logger_securite.error("Pièce jointe référencée mais absente du stockage (id=%s).", attachment.pk)
            raise Http404("Ce fichier n'est plus disponible.")
        return FileResponse(contenu, filename=attachment.file.name.rsplit("/", 1)[-1])
