from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.urls import reverse
from django.utils.html import format_html

from .models import Utilisateur, DocumentKYC


class UtilisateurAdmin(UserAdmin):
    """
    Personnalisation de l'administration Django
    pour le modèle Utilisateur.
    """

    model = Utilisateur

    # Colonnes affichées dans la liste des utilisateurs.
    list_display = (
        'email',
        'nom',
        'prenom',
        'role',
        'statut_kyc',
        'is_active',
        'is_staff',
    )

    # Filtres disponibles dans l'interface d'administration.
    list_filter = (
        'role',
        'statut_kyc',
        'is_active',
        'is_staff',
    )

    # Champs utilisés par le moteur de recherche.
    search_fields = (
        'email',
        'nom',
        'prenom',
        'telephone',
    )

    # Tri par défaut.
    ordering = ('email',)

    # Organisation des champs lors de la modification
    # d'un utilisateur existant.
    fieldsets = (
        (None, {
            'fields': (
                'email',
                'password',
            )
        }),

        ('Informations personnelles', {
            'fields': (
                'nom',
                'prenom',
                'telephone',
            )
        }),

        ('Rôle et statut', {
            'fields': (
                'role',
                'statut_kyc',
                'email_verifie',
                'telephone_verifie',
            )
        }),

        ('Permissions', {
            'fields': (
                'is_active',
                'is_staff',
                'is_superuser',
                'groups',
                'user_permissions',
            )
        }),

        ('Dates importantes', {
            'fields': (
                'last_login',
                'date_creation',
                'date_mise_a_jour',
            )
        }),
    )

    # Champs affichés lors de la création
    # d'un nouvel utilisateur depuis l'administration.
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': (
                'email',
                'nom',
                'prenom',
                'password1',
                'password2',
            ),
        }),
    )

    # Champs consultables mais non modifiables.
    readonly_fields = (
        # F-16 : 'statut_kyc' en lecture seule ici. La validation d'un
        # vendeur doit passer par apps.vendeurs.admin.DemandeVendeurAdmin
        # (actions valider_les_demandes / refuser_les_demandes), qui
        # appelle valider_demande_vendeur() — verrouillage + cohérence
        # atomique entre role et statut_kyc. Une édition libre de
        # statut_kyc seul depuis CET admin (vue générale de tous les
        # utilisateurs) ne débloque aucun privilège (EstVendeurValide
        # vérifie role ET statut_kyc ensemble), mais peut créer un état
        # de données incohérent (ex: KYC validé, rôle toujours CLIENT).
        'statut_kyc',
        'date_creation',
        'date_mise_a_jour',
        'last_login',
    )


# Enregistrement des modèles dans l'administration Django.
admin.site.register(Utilisateur, UtilisateurAdmin)


@admin.register(DocumentKYC)
class DocumentKYCAdmin(admin.ModelAdmin):
    """
    SÉCURITÉ (Broken Access Control) : `piece_identite_recto`,
    `piece_identite_verso` et `selfie` sont volontairement exclus du
    formulaire admin ci-dessous. Un enregistrement nu
    (`admin.site.register(DocumentKYC)`, sans ModelAdmin) rendrait ces
    FileField comme des liens directs vers leur MEDIA_URL brute — exactement
    la fuite que TelechargerDocumentKYCView et les champs write_only du
    serializer existent pour fermer côté API publique, mais réouverte ici
    par un chemin d'accès différent. Passer ces champs en `readonly_fields`
    plutôt qu'`exclude` ne suffit pas non plus : Django affiche toujours un
    lien cliquable vers le fichier pour un FileField en lecture seule.
    À la place, trois méthodes ci-dessous fournissent un lien qui passe par
    la vue authentifiée existante (contrôle propriétaire/admin déjà en
    place), jamais par l'URL MEDIA directe.
    """

    list_display = ('utilisateur', 'type_piece', 'date_soumission', 'date_traitement')
    readonly_fields = (
        'utilisateur',
        'type_piece',
        'numero_mobile_money',
        'adresse',
        'compte_bancaire',
        'date_soumission',
        'date_traitement',
        'lien_piece_identite_recto',
        'lien_piece_identite_verso',
        'lien_selfie',
    )
    exclude = ('piece_identite_recto', 'piece_identite_verso', 'selfie')
    search_fields = ('utilisateur__email',)

    def lien_piece_identite_recto(self, obj):
        return self._lien_document(obj, 'piece_identite_recto')
    lien_piece_identite_recto.short_description = "Pièce d'identité (recto)"

    def lien_piece_identite_verso(self, obj):
        return self._lien_document(obj, 'piece_identite_verso')
    lien_piece_identite_verso.short_description = "Pièce d'identité (verso)"

    def lien_selfie(self, obj):
        return self._lien_document(obj, 'selfie')
    lien_selfie.short_description = "Selfie"

    def _lien_document(self, obj, champ):
        if not getattr(obj, champ):
            return "—"
        url = reverse('kyc-telecharger', args=[obj.utilisateur_id, champ])
        return format_html('<a href="{}">Télécharger (accès contrôlé)</a>', url)