from django.urls import path

from . import views

app_name = "paiements"

urlpatterns = [
    # Client
    path("initier/", views.InitierPaiementView.as_view(), name="initier-paiement"),
    path("", views.PaiementListView.as_view(), name="paiement-liste"),
    path("<uuid:pk>/", views.PaiementDetailView.as_view(), name="paiement-detail"),
    path("<uuid:pk>/annuler/", views.AnnulerPaiementView.as_view(), name="paiement-annuler"),
    # Fournisseurs
    path("webhook/<str:fournisseur>/", views.WebhookPaiementView.as_view(), name="webhook-paiement"),
    path("webhook/<str:fournisseur>/transfert/", views.WebhookTransfertView.as_view(), name="webhook-transfert"),
    # Vendeur
    path("vendeur/reversements/", views.ReversementVendeurListView.as_view(), name="vendeur-reversements"),
    path("vendeur/reversements/resume/", views.ResumeReversementsVendeurView.as_view(),
         name="vendeur-reversements-resume"),
    # Administration
    path("admin/remboursements/", views.RemboursementAdminListView.as_view(), name="admin-remboursements"),
    path("admin/remboursements/<uuid:pk>/traiter/", views.TraiterRemboursementView.as_view(),
         name="admin-remboursement-traiter"),
    path("admin/reversements/", views.ReversementAdminListView.as_view(), name="admin-reversements"),
    path("admin/reversements/<uuid:pk>/verser/", views.VerserReversementView.as_view(),
         name="admin-reversement-verser"),
    path("admin/reversements/<uuid:pk>/transferer/", views.TransfererReversementView.as_view(),
         name="admin-reversement-transferer"),
    path("admin/baremes/", views.BaremeFraisListCreateView.as_view(), name="admin-baremes"),
    path("admin/baremes/<uuid:pk>/", views.BaremeFraisDetailView.as_view(), name="admin-bareme-detail"),
]
