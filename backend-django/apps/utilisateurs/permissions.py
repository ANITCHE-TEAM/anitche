"""Permissions du domaine utilisateurs, réutilisables par les autres modules."""

from rest_framework.permissions import BasePermission

#: Code machine renvoyé dans `errors.code` : le frontend s'y fie pour
#: proposer la vérification de l'email, sans analyser le texte du message.
CODE_EMAIL_NON_VERIFIE = 'email_non_verifie'


class EmailVerifie(BasePermission):
    """Action sensible réservée aux comptes dont l'email est vérifié (OTP).

    La connexion reste possible sans email vérifié (navigation, panier) ;
    sont bloquées : dépôt KYC (qui vaut demande vendeur), changement de
    contact, validation du panier. Le code d'inscription se redemande via
    renvoyer-code-inscription/.
    """

    message = {
        'detail': (
            "Vérifiez d'abord votre adresse email : saisissez le code reçu à "
            "l'inscription, ou demandez-en un nouveau."
        ),
        'code': CODE_EMAIL_NON_VERIFIE,
    }

    def has_permission(self, request, view):
        utilisateur = request.user
        return bool(utilisateur and utilisateur.is_authenticated and utilisateur.email_verifie)
