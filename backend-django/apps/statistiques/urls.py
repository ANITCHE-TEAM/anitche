from django.urls import path

from .views import StatistiquesAdministrationView, StatistiquesVendeurView

app_name = 'statistiques'

urlpatterns = [
    # Lecture seule : la boutique du vendeur connecté, ou la plateforme (administration).
    path('vendeur/', StatistiquesVendeurView.as_view(), name='vendeur'),
    path('administration/', StatistiquesAdministrationView.as_view(), name='administration'),
]
