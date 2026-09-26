"""Interface commune des fournisseurs de paiement.

Le reste du module ne parle qu'à cette interface : changer de fournisseur
(CinetPay aujourd'hui, PayDunya en réserve) revient à écrire un nouvel
adaptateur, sans toucher aux commandes, aux remboursements ni aux
reversements. Procédure : docs/MODULE_PAIEMENTS.md, « Ajouter un fournisseur ».

Règle de sécurité commune à tous les adaptateurs : une notification n'est
qu'un signal. Elle doit être authentifiée (lire_notification puis
authentifier_notification), puis l'état réel de la transaction est
redemandé au fournisseur (verifier_transaction) avant toute validation.
"""

import hashlib
import hmac
from dataclasses import dataclass, field


SUCCES = "succes"
ECHEC = "echec"
EN_ATTENTE = "en_attente"


class ErreurFournisseur(Exception):
    """Le fournisseur est injoignable ou a refusé l'opération."""


class MontantHorsLimites(ErreurFournisseur):
    """Le montant sort des limites acceptées par le fournisseur."""


class NotificationInvalide(Exception):
    """Notification non authentique, incomplète ou incohérente."""


class OperationNonSupportee(Exception):
    """Le fournisseur ne propose pas cette opération par API."""


@dataclass(frozen=True)
class SessionPaiement:
    """Résultat de l'initiation : où envoyer le client."""

    url_paiement: str
    identifiant_externe: str
    # Jeton propre à la transaction, renvoyé dans ses notifications ; seule
    # son empreinte est conservée (hacher_jeton).
    jeton_notification: str = ""


@dataclass(frozen=True)
class Notification:
    """Notification lue, pas encore vérifiée auprès du fournisseur."""

    reference: str
    identifiant_externe: str
    # Clé d'idempotence (JournalWebhook) : la même notification reçue deux
    # fois n'est traitée qu'une fois.
    cle_idempotence: str
    jeton: str = ""
    donnees: dict = field(default_factory=dict)


@dataclass(frozen=True)
class EtatTransaction:
    """État d'une transaction tel que confirmé par le fournisseur."""

    statut: str  # SUCCES, ECHEC ou EN_ATTENTE
    identifiant_externe: str = ""
    # None : le fournisseur ne renvoie pas l'information à la vérification
    # (le montant a alors été fixé côté serveur à l'initiation).
    montant: int | None = None
    devise: str | None = None


@dataclass(frozen=True)
class ResultatTransfert:
    """Réponse du fournisseur à un transfert vers un vendeur."""

    statut: str  # SUCCES, ECHEC ou EN_ATTENTE (état final par notification)
    identifiant_externe: str = ""
    jeton_notification: str = ""


def hacher_jeton(jeton):
    """Empreinte stockée à la place du jeton de notification."""
    return hashlib.sha256(jeton.encode()).hexdigest() if jeton else ""


def jeton_valide(jeton_recu, empreinte_attendue):
    """Comparaison à temps constant du jeton reçu avec l'empreinte stockée."""
    if not jeton_recu or not empreinte_attendue:
        return False
    return hmac.compare_digest(hacher_jeton(jeton_recu), empreinte_attendue)


class FournisseurPaiement:
    """Contrat d'un adaptateur. Chaque méthode lève ErreurFournisseur si le
    fournisseur est injoignable, NotificationInvalide si une notification
    n'est pas authentique, OperationNonSupportee si l'opération n'existe pas
    chez lui."""

    code = ""

    # --- Encaissement -----------------------------------------------------

    def initier(self, paiement):
        """Ouvre la transaction chez le fournisseur → SessionPaiement."""
        raise NotImplementedError

    def lire_notification(self, request):
        """Lit la notification reçue → Notification (sans la faire confiance)."""
        raise NotImplementedError

    def authentifier_notification(self, notification, objet):
        """Vérifie que la notification vient du fournisseur pour CE paiement
        (ou CE reversement) : jeton comparé à l'empreinte stockée."""
        if not jeton_valide(notification.jeton, objet.hash_jeton_notification):
            raise NotificationInvalide("Jeton de notification invalide.")

    def verifier_transaction(self, paiement, notification=None):
        """Redemande au fournisseur l'état réel de la transaction → EtatTransaction."""
        raise NotImplementedError

    def rembourser(self, remboursement):
        """Rembourse le client par API (sinon : traitement manuel par l'admin)."""
        raise OperationNonSupportee(f"Remboursement par API non disponible chez {self.code}.")

    # --- Reversements (transferts vers les vendeurs) ------------------------

    def transferer(self, reversement, telephone, operateur):
        """Envoie reversement.montant_a_verser sur le mobile money du vendeur
        → ResultatTransfert (l'état final peut arriver par notification)."""
        raise OperationNonSupportee(f"Transfert par API non disponible chez {self.code}.")

    def lire_notification_transfert(self, request):
        return self.lire_notification(request)

    def verifier_transfert(self, reversement, notification=None):
        """État réel du transfert → EtatTransaction."""
        raise OperationNonSupportee(f"Transfert par API non disponible chez {self.code}.")
