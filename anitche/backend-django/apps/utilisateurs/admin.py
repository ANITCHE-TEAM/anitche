from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

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
        'date_creation',
        'date_mise_a_jour',
        'last_login',
    )


# Enregistrement des modèles dans l'administration Django.
admin.site.register(Utilisateur, UtilisateurAdmin)
admin.site.register(DocumentKYC)