from django.urls import path

from .views import ArticlesANoterListView, AvisCreerView, AvisDetailView, MesAvisListView

app_name = "avis"

urlpatterns = [
    path("", AvisCreerView.as_view(), name="avis-creer"),
    path("a-noter/", ArticlesANoterListView.as_view(), name="avis-a-noter"),
    path("mes-avis/", MesAvisListView.as_view(), name="avis-mes-avis"),
    path("<uuid:pk>/", AvisDetailView.as_view(), name="avis-detail"),
]
