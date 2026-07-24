from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import Utilisateur, DocumentKYC

class UtilisateurAdmin(UserAdmin):
    model = Utilisateur 
    list_display = ('email', 'nom', 'prenom', 'role', 'statut_kyc', 'is_active', 'is_staff')
    list_filter = ('role', 'statut_kyc', 'is_active', 'is_staff')
    search_fields = ('email', 'nom', 'prenom', 'telephone')
    ordering = ('email',)


    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Informations personnelles', {'fields': ('nom', 'prenom', 'telephone')}),
        ('Rôle et statut', {'fields': ('role', 'statut_kyc', 'email_verifie', 'telephone_verifie')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Dates importantes', {'fields': ('last_login', 'date_creation', 'date_mise_a_jour')}),
    )

    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'nom', 'prenom', 'password1', 'password2'),
        }),
    )

    readonly_fields = ('date_creation', 'date_mise_a_jour', 'last_login')


admin.site.register(Utilisateur, UtilisateurAdmin)
admin.site.register(DocumentKYC)
