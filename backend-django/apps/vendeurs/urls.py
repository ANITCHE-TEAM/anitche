from django.urls import path

from .views import (
    BoutiqueAdministrationDetailView,
    BoutiquePubliqueDetailView,
    BoutiquePubliqueListView,
    BoutiquesAdministrationListView,
    DemandesVendeurListView,
    MaBoutiqueView,
    RefuserDemandeVendeurView,
    ValiderDemandeVendeurView,
)

app_name = 'vendeurs'

urlpatterns = [
    # Public
    path('boutiques/', BoutiquePubliqueListView.as_view(), name='boutiques-liste'),
    path('boutiques/<slug:slug>/', BoutiquePubliqueDetailView.as_view(), name='boutique-detail'),

    # Vendeur authentifié
    path('ma-boutique/', MaBoutiqueView.as_view(), name='ma-boutique'),

    # Administration
    path(
        'administration/demandes/',
        DemandesVendeurListView.as_view(),
        name='administration-demandes',
    ),
    path(
        'administration/demandes/<int:pk>/valider/',
        ValiderDemandeVendeurView.as_view(),
        name='administration-demande-valider',
    ),
    path(
        'administration/demandes/<int:pk>/refuser/',
        RefuserDemandeVendeurView.as_view(),
        name='administration-demande-refuser',
    ),
    path(
        'administration/boutiques/',
        BoutiquesAdministrationListView.as_view(),
        name='administration-boutiques',
    ),
    path(
        'administration/boutiques/<int:pk>/',
        BoutiqueAdministrationDetailView.as_view(),
        name='administration-boutique-detail',
    ),
]
