from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers
from django.shortcuts import get_object_or_404
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from .models import Notification, PreferenceNotification
from .serializers import NotificationSerializer, PreferenceNotificationSerializer
from .services import ServiceNotification


@extend_schema(
    summary="Mes notifications",
    parameters=[
        OpenApiParameter("non_lues", bool, description="true : seulement les non lues."),
        OpenApiParameter("type", enum=[valeur for valeur, _ in Notification.TypeNotification.choices]),
    ],
)
class NotificationListView(generics.ListAPIView):
    """Liste paginée des notifications de l'utilisateur connecté."""

    permission_classes = [IsAuthenticated]
    serializer_class = NotificationSerializer

    def get_queryset(self):
        user = self.request.user
        qs = Notification.objects.filter(destinataire=user)

        # Filtre optionnel pour n'afficher que les notifications non lues (?non_lues=true)
        non_lues = self.request.query_params.get("non_lues", "").lower() in ("true", "1")
        if non_lues:
            qs = qs.filter(est_lu=False)

        # Filtre optionnel par type (?type=commande, ?type=livraison, etc.)
        type_notif = self.request.query_params.get("type")
        if type_notif:
            qs = qs.filter(type_notification=type_notif)

        return qs


@extend_schema(
    summary="Nombre de notifications non lues",
    responses={200: inline_serializer("CompteurNonLues", {"non_lues": serializers.IntegerField()})},
)
class NotificationCompteurNonLuesView(APIView):
    """Retourne le nombre de notifications non lues de l'utilisateur connecté."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        nb_non_lues = Notification.objects.filter(
            destinataire=request.user,
            est_lu=False,
        ).count()
        return Response({"non_lues": nb_non_lues}, status=status.HTTP_200_OK)


@extend_schema(
    summary="Marquer une notification comme lue",
    request=None,
    responses={200: NotificationSerializer},
)
class NotificationMarquerLueView(APIView):
    """Marque une notification spécifique comme lue."""

    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        notification = get_object_or_404(Notification, pk=pk, destinataire=request.user)
        notification.marquer_comme_lue()
        return Response(NotificationSerializer(notification).data, status=status.HTTP_200_OK)


@extend_schema(
    summary="Marquer toutes les notifications comme lues",
    request=None,
    responses={200: inline_serializer(
        "NotificationsMarqueesLues",
        {"message": serializers.CharField(), "nb_modifiees": serializers.IntegerField()},
    )},
)
class NotificationMarquerToutesLuesView(APIView):
    """Marque toutes les notifications non lues de l'utilisateur connecté comme lues."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        nb_modifiees = ServiceNotification.marquer_toutes_lues(request.user)
        return Response(
            {
                "message": "Toutes les notifications ont été marquées comme lues.",
                "nb_modifiees": nb_modifiees,
            },
            status=status.HTTP_200_OK,
        )


class PreferenceNotificationView(generics.RetrieveUpdateAPIView):
    """Consultation et modification des préférences de notification de l'utilisateur connecté."""

    permission_classes = [IsAuthenticated]
    serializer_class = PreferenceNotificationSerializer

    def get_object(self):
        return ServiceNotification.get_preferences(self.request.user)
