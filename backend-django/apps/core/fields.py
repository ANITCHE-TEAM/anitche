"""
Champ de modèle pour chiffrer au repos des données sensibles (ex: IBAN /
numéro de compte bancaire, numéro mobile money) qui n'ont besoin d'être ni
recherchées, ni indexées en base — seulement lues en clair côté application
quand un utilisateur autorisé les consulte.

Pourquoi pas un hash (comme CodeOTP.code_hash) : un hash est irréversible
et convient à une vérification d'égalité (un OTP est comparé, jamais
réaffiché). Un compte bancaire doit au contraire pouvoir être redéchiffré
pour être présenté à l'utilisateur ou transmis à un partenaire de paiement
— d'où un chiffrement symétrique réversible (Fernet/AES) plutôt qu'un hash.

Le chiffrement protège contre une fuite de la base de données seule (dump,
backup mal sécurisé, requête SQL via une injection résiduelle) : sans les
clés (FIELD_ENCRYPTION_KEYS, en variable d'environnement, jamais en base ni
dans ses sauvegardes), les valeurs chiffrées sont inexploitables.

Clés (MultiFernet) : la PREMIÈRE clé de FIELD_ENCRYPTION_KEYS chiffre, toutes
déchiffrent. Rotation : ajouter la nouvelle clé en tête, lancer
`manage.py rechiffrer_donnees_sensibles`, puis retirer l'ancienne. Voir
docs/MODULE_UTILISATEURS.md, « Gestion de la clé ».
"""

import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

logger_securite = logging.getLogger('securite')

MESSAGE_VALEUR_ILLISIBLE = "[valeur illisible — clé de chiffrement invalide ou modifiée]"


def cles_de_chiffrement():
    """Clés configurées, la clé active en premier.

    FIELD_ENCRYPTION_KEYS (liste) prime ; à défaut, FIELD_ENCRYPTION_KEY
    (clé unique, compatibilité).
    """
    cles = [cle for cle in getattr(settings, 'FIELD_ENCRYPTION_KEYS', None) or [] if cle]
    if not cles and getattr(settings, 'FIELD_ENCRYPTION_KEY', None):
        cles = [settings.FIELD_ENCRYPTION_KEY]
    if not cles:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEYS doit contenir au moins une clé pour utiliser un "
            "champ chiffré (EncryptedCharField). Générez-en une avec : "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    return cles


def chiffreur():
    return MultiFernet([Fernet(cle.encode() if isinstance(cle, str) else cle) for cle in cles_de_chiffrement()])


class ValeurIllisible(str):
    """Valeur qu'aucune clé configurée ne sait déchiffrer.

    S'affiche comme un message explicite, mais garde le jeton chiffré
    d'origine : si l'instance est sauvegardée, c'est ce jeton qui est
    réécrit tel quel. Une clé perdue ou erronée ne détruit donc jamais la
    donnée (elle redevient lisible dès que la bonne clé est remise).
    """

    def __new__(cls, jeton_chiffre):
        valeur = super().__new__(cls, MESSAGE_VALEUR_ILLISIBLE)
        valeur.jeton_chiffre = jeton_chiffre
        return valeur


class EncryptedCharField(models.TextField):
    """
    Stocke une chaîne chiffrée (Fernet) en base ; expose la valeur en clair
    côté Python. Basé sur TextField (et non CharField) car un jeton Fernet
    est nettement plus long que le texte source une fois chiffré/encodé en
    base64 — imposer un max_length côté DB casserait silencieusement le
    stockage de valeurs pourtant valides côté métier. La longueur métier est
    validée par les serializers.

    Ne PAS utiliser pour des champs qu'on a besoin de filtrer/rechercher en
    base (WHERE compte_bancaire=...) : le chiffrement Fernet n'est pas
    déterministe (le même texte clair donne un jeton différent à chaque
    appel), donc aucune requête d'égalité ne peut fonctionner sur la colonne
    chiffrée.
    """

    description = "Texte chiffré au repos (Fernet)"

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        try:
            return chiffreur().decrypt(value.encode()).decode()
        except (InvalidToken, ValueError):
            logger_securite.error(
                "Champ chiffré illisible (%s) : aucune clé de FIELD_ENCRYPTION_KEYS ne le déchiffre.",
                getattr(self, 'name', '?'),
            )
            return ValeurIllisible(value)

    def to_python(self, value):
        return value

    def get_prep_value(self, value):
        if value is None or value == "":
            return value
        if isinstance(value, ValeurIllisible):
            return value.jeton_chiffre
        return chiffreur().encrypt(str(value).encode()).decode()
