from django.urls import path
from .views import (
    NotificationListView,
    NotificationCompteurNonLuesView,
    NotificationMarquerLueView,
    NotificationMarquerToutesLuesView,
    PreferenceNotificationView,
)

app_name = "notifications"

urlpatterns = [
    path("", NotificationListView.as_view(), name="notification-liste"),
    path("compteur/", NotificationCompteurNonLuesView.as_view(), name="notification-compteur"),
    path("<uuid:pk>/lire/", NotificationMarquerLueView.as_view(), name="notification-marquer-lue"),
    path("toutes-lues/", NotificationMarquerToutesLuesView.as_view(), name="notification-toutes-lues"),
    path("preferences/", PreferenceNotificationView.as_view(), name="notification-preferences"),
]
