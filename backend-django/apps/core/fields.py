"""
Champ de modèle pour chiffrer au repos des données sensibles (ex: IBAN /
numéro de compte bancaire) qui n'ont besoin d'être ni recherchées, ni
indexées en base — seulement lues en clair côté application quand un
utilisateur autorisé les consulte.

Pourquoi pas un hash (comme CodeOTP.code_hash) : un hash est irréversible
et convient à une vérification d'égalité (un OTP est comparé, jamais
réaffiché). Un compte bancaire doit au contraire pouvoir être redéchiffré
pour être présenté à l'utilisateur ou transmis à un partenaire de paiement
— d'où un chiffrement symétrique réversible (Fernet/AES) plutôt qu'un hash.

Le chiffrement protège contre une fuite de la base de données seule (dump,
backup mal sécurisé, requête SQL via une injection résiduelle) : sans la
clé FIELD_ENCRYPTION_KEY (qui vit en variable d'environnement, jamais en
base), les valeurs chiffrées sont inexploitables.
"""

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models


def _fernet():
    cle = getattr(settings, "FIELD_ENCRYPTION_KEY", None)
    if not cle:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEY doit être défini pour utiliser un champ "
            "chiffré (EncryptedCharField). Générez-en une avec : "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    return Fernet(cle.encode() if isinstance(cle, str) else cle)


class EncryptedCharField(models.TextField):
    """
    Stocke une chaîne chiffrée (Fernet) en base ; expose la valeur en clair
    côté Python. Basé sur TextField (et non CharField) car un jeton Fernet
    est nettement plus long que le texte source une fois chiffré/encodé en
    base64 — imposer un max_length côté DB casserait silencieusement le
    stockage de valeurs pourtant valides côté métier.

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
            return _fernet().decrypt(value.encode()).decode()
        except (InvalidToken, ValueError):
            # Valeur illisible (mauvaise clé, donnée corrompue, ou valeur
            # historique jamais chiffrée) : on ne fait jamais planter une
            # lecture sur ce champ secondaire, on renvoie une valeur
            # explicite plutôt que de faire remonter le clair par erreur.
            return "[valeur illisible — clé de chiffrement invalide ou modifiée]"

    def to_python(self, value):
        return value

    def get_prep_value(self, value):
        if value is None or value == "":
            return value
        # Idempotent-safe : si la valeur est déjà un jeton Fernet valide
        # (ex: relecture d'une instance sans modification du champ), on ne
        # la re-chiffre pas une seconde fois.
        try:
            _fernet().decrypt(value.encode())
            return value
        except (InvalidToken, ValueError):
            return _fernet().encrypt(value.encode()).decode()
