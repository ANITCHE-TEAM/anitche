
import logging
from contextlib import contextmanager

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)

from apps.core.reseau import adresse_ip_client

from .models import (
    ADRESSES_MAX_PAR_CLIENT,
    AdresseClient,
    JournalConnexion,
    MethodeConnexion,
    MotifEchecConnexion,
    Utilisateur,
)

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

    Les access tokens (durée de vie courte, 15 minutes : SIMPLE_JWT) ne
    sont pas mis en liste noire : simplejwt ne blackliste que les refresh
    tokens (seule la classe RefreshToken hérite de BlacklistMixin). Après
    un changement de mot de passe, ils sont de toute façon refusés grâce à
    CHECK_REVOKE_TOKEN.
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

    def __init__(self, utilisateur=None):
        super().__init__()
        self.utilisateur = utilisateur


class LiaisonGoogleRefusee(Exception):
    """
    Liaison automatique refusée pour éviter un pré-hijacking de compte :
    un compte existant non vérifié et possédant un mot de passe
    utilisable ne peut pas être lié automatiquement à un identifiant
    Google, sans quoi l'attaquant qui l'a créé garderait un accès total.
    """

    def __init__(self, utilisateur=None):
        super().__init__()
        self.utilisateur = utilisateur


def resoudre_utilisateur_google(infos):
    """
    Détermine l'utilisateur correspondant aux informations d'un token
    Google Identity Services déjà vérifié, en le créant ou en le liant
    à un compte existant si nécessaire.

    Ne délivre aucun token : cette fonction se limite à la résolution
    du compte. L'émission du JWT reste de la responsabilité de la vue.
    """
    google_id = infos.get('sub')
    # Même forme qu'à l'inscription classique : minuscules.
    email = Utilisateur.objects.normalize_email(infos.get('email'))
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
        utilisateur_existant = Utilisateur.objects.avec_email(email).first()

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
            raise CompteDesactive(utilisateur_existant)

        if (
            utilisateur_existant
            and not utilisateur_existant.email_verifie
            and utilisateur_existant.has_usable_password()
        ):
            logger_securite.warning(
                "Liaison Google refusée : compte existant non vérifié "
                "(email=%s).", email,
            )
            raise LiaisonGoogleRefusee(utilisateur_existant)

        utilisateur, cree = Utilisateur.objects.avec_email(email).get_or_create(
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
        raise CompteDesactive(utilisateur)

    return utilisateur


# =====================================================
# JOURNAL DES CONNEXIONS
# =====================================================

USER_AGENT_LONGUEUR_MAX = 255
EMAIL_TENTE_LONGUEUR_MAX = 254


def _texte_pour_journal(valeur, longueur_max):
    """Texte tronqué, sans caractère nul (refusé par PostgreSQL)."""
    if not isinstance(valeur, str):
        return ''
    return valeur.replace('\x00', '')[:longueur_max]


@contextmanager
def _journal_sans_echec(methode):
    """Absorbe toute panne du journal des connexions.

    Une panne (base indisponible, valeur refusée) est journalisée puis
    ignorée : elle ne doit transformer ni une connexion réussie ni un refus en
    erreur serveur. Le bloc s'exécute dans un point de sauvegarde, pour ne pas
    rendre inutilisable une transaction déjà ouverte par l'appelant.
    """
    try:
        with transaction.atomic():
            yield
    except Exception:
        logger_securite.exception("Écriture du journal des connexions impossible (methode=%s)", methode)


def enregistrer_connexion(request, *, methode, reussie, utilisateur=None, email='', motif_echec=''):
    """Écrit une ligne du journal des connexions ; n'échoue jamais.

    L'adresse IP vient de `adresse_ip_client` (la même source que les limites
    de débit et l'email de nouvelle connexion) : un X-Forwarded-For envoyé par
    le client n'est jamais lu, sauf derrière le proxy de confiance déclaré par
    REST_FRAMEWORK['NUM_PROXIES'].
    """
    with _journal_sans_echec(methode):
        JournalConnexion.objects.create(
            utilisateur=utilisateur,
            email_tente=_texte_pour_journal(email, EMAIL_TENTE_LONGUEUR_MAX).strip().lower(),
            reussie=reussie,
            motif_echec='' if reussie else motif_echec,
            methode=methode,
            adresse_ip=adresse_ip_client(request),
            user_agent=_texte_pour_journal(request.META.get('HTTP_USER_AGENT', ''), USER_AGENT_LONGUEUR_MAX),
        )


def enregistrer_echec_mot_de_passe(request, email):
    """Journalise un refus de la connexion par email et mot de passe.

    Le motif est déduit du compte visé ; la réponse faite au client, elle, est
    la même dans les trois cas (voir LoginThrottleView).
    """
    email = _texte_pour_journal(email, EMAIL_TENTE_LONGUEUR_MAX)
    with _journal_sans_echec(MethodeConnexion.MOT_DE_PASSE):
        utilisateur = Utilisateur.objects.avec_email(email).first()
        if utilisateur is None:
            motif = MotifEchecConnexion.EMAIL_INCONNU
        elif not utilisateur.is_active:
            motif = MotifEchecConnexion.COMPTE_DESACTIVE
        else:
            motif = MotifEchecConnexion.MOT_DE_PASSE_INCORRECT
        enregistrer_connexion(
            request, methode=MethodeConnexion.MOT_DE_PASSE, reussie=False,
            utilisateur=utilisateur, email=email, motif_echec=motif,
        )


# =====================================================
# CARNET D'ADRESSES
# =====================================================

CODE_CARNET_PLEIN = 'carnet_plein'


def _verrouiller_carnet(client):
    """Sérialise les écritures du carnet d'un même client (limite de taille,
    adresse par défaut) : deux requêtes simultanées s'exécutent l'une après
    l'autre au lieu de lire toutes deux le même état."""
    Utilisateur.objects.select_for_update().get(pk=client.pk)


def creer_adresse(client, donnees):
    """Ajoute une adresse au carnet du client. `par_defaut` fait de cette
    adresse la seule par défaut."""
    with transaction.atomic():
        _verrouiller_carnet(client)
        if AdresseClient.objects.filter(client=client).count() >= ADRESSES_MAX_PAR_CLIENT:
            raise ValidationError({
                'detail': f"Votre carnet contient déjà {ADRESSES_MAX_PAR_CLIENT} adresses : supprimez-en une pour en ajouter.",
                'code': CODE_CARNET_PLEIN,
            })
        if donnees.get('par_defaut'):
            AdresseClient.objects.filter(client=client, par_defaut=True).update(par_defaut=False)
        return AdresseClient.objects.create(client=client, **donnees)


def modifier_adresse(adresse, donnees):
    """Met à jour une adresse du carnet. `par_defaut` à vrai en fait la seule
    par défaut du client ; à faux, le client n'a plus d'adresse par défaut."""
    with transaction.atomic():
        _verrouiller_carnet(adresse.client)
        if donnees.get('par_defaut'):
            AdresseClient.objects.filter(client=adresse.client, par_defaut=True).exclude(pk=adresse.pk).update(
                par_defaut=False,
            )
        for champ, valeur in donnees.items():
            setattr(adresse, champ, valeur)
        adresse.save()
        return adresse


def adresse_du_checkout(client, adresse_id):
    """Adresse du carnet du client, au format de l'adresse saisie au checkout
    (la commande en garde une copie). 404 quand elle n'existe pas ou n'est pas
    la sienne : les deux cas sont indiscernables."""
    adresse = get_object_or_404(AdresseClient, pk=adresse_id, client=client)
    return {
        'commune': adresse.commune,
        'quartier': adresse.quartier,
        'point_de_repere': adresse.point_de_repere,
        'telephone': adresse.telephone,
        'destinataire': adresse.destinataire,
        'indications': adresse.indications,
        'latitude': adresse.latitude,
        'longitude': adresse.longitude,
    }
