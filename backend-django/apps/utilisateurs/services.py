
import logging

from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)

from .models import Utilisateur

logger_securite = logging.getLogger('securite')


# =====================================================
# RÉVOCATION DE TOKENS
# =====================================================

def revoquer_tokens_actifs(utilisateur):
    """
    Blackliste tous les refresh tokens actifs de l'utilisateur.

    Utilisé après une réinitialisation de mot de passe : le scénario
    visé (compte potentiellement compromis) exige qu'un refresh token
    déjà émis à un attaquant ne reste pas valable jusqu'à 7 jours après
    le changement de mot de passe.

    Les access tokens (durée de vie courte, 30 minutes) ne sont pas
    concernés : simplejwt ne blackliste que les refresh tokens
    (seule la classe RefreshToken hérite de BlacklistMixin).
    """
    tokens = OutstandingToken.objects.filter(user=utilisateur)
    BlacklistedToken.objects.bulk_create(
        (BlacklistedToken(token=jeton) for jeton in tokens),
        ignore_conflicts=True,
    )


# =====================================================
# CONNEXION GOOGLE
# =====================================================

class InfosGoogleIncompletes(Exception):
    """Le token Google ne fournit pas les informations minimales requises."""
    pass


class CompteDesactive(Exception):
    """Le compte visé a été désactivé (banni, fraude...)."""
    pass


class LiaisonGoogleRefusee(Exception):
    """
    Liaison automatique refusée pour éviter un pré-hijacking de compte :
    un compte existant non vérifié et possédant un mot de passe
    utilisable ne peut pas être lié automatiquement à un identifiant
    Google, sans quoi l'attaquant qui l'a créé garderait un accès total.
    """
    pass


def resoudre_utilisateur_google(infos):
    """
    Détermine l'utilisateur correspondant aux informations d'un token
    Google Identity Services déjà vérifié, en le créant ou en le liant
    à un compte existant si nécessaire.

    Ne délivre aucun token : cette fonction se limite à la résolution
    du compte. L'émission du JWT reste de la responsabilité de la vue.
    """
    google_id = infos.get('sub')
    email = infos.get('email')
    email_verifie_google = infos.get('email_verified', False)

    if not google_id or not email or not email_verifie_google:
        raise InfosGoogleIncompletes(
            "Informations Google incomplètes ou non vérifiées."
        )

    # 1. Compte déjà lié à ce google_id -> résolution directe.
    utilisateur = Utilisateur.objects.filter(google_id=google_id).first()

    if not utilisateur:
        # 2. Sinon, un compte existe peut-être déjà avec cet email
        #    (inscription classique) -> on le lie à Google.
        utilisateur_existant = Utilisateur.objects.filter(
            email__iexact=email
        ).first()

        # Un compte désactivé doit être bloqué en priorité absolue,
        # avant toute autre logique de liaison — y compris la
        # protection anti-pré-hijacking ci-dessous, qui sinon
        # renverrait à tort une erreur de liaison au lieu d'un
        # compte désactivé.
        if utilisateur_existant and not utilisateur_existant.is_active:
            logger_securite.warning(
                "Tentative de connexion Google refusée : compte désactivé "
                "(utilisateur_id=%s, email=%s)",
                utilisateur_existant.id, utilisateur_existant.email,
            )
            raise CompteDesactive()

        if (
            utilisateur_existant
            and not utilisateur_existant.email_verifie
            and utilisateur_existant.has_usable_password()
        ):
            logger_securite.warning(
                "Liaison Google refusée : compte existant non vérifié "
                "(email=%s).", email,
            )
            raise LiaisonGoogleRefusee()

        utilisateur, cree = Utilisateur.objects.get_or_create(
            email__iexact=email,
            defaults={
                'email': email,
                'nom': infos.get('family_name', ''),
                'prenom': infos.get('given_name', ''),
                'email_verifie': True,
                'google_id': google_id,
            },
        )

        if cree:
            utilisateur.set_unusable_password()
            utilisateur.save(update_fields=['password'])
        else:
            utilisateur.google_id = google_id
            if not utilisateur.email_verifie:
                utilisateur.email_verifie = True
            utilisateur.save(update_fields=['google_id', 'email_verifie'])

    # Contrôle d'accès final : un compte désactivé ne doit jamais
    # recevoir de token, quel que soit le moyen de connexion.
    # RefreshToken.for_user() ne fait volontairement AUCUNE vérification
    # d'is_active (contrairement à authenticate()) donc ce garde-fou
    # doit être posé explicitement ici.
    if not utilisateur.is_active:
        logger_securite.warning(
            "Tentative de connexion Google refusée : compte désactivé "
            "(utilisateur_id=%s, email=%s)",
            utilisateur.id, utilisateur.email,
        )
        raise CompteDesactive()

    return utilisateur
