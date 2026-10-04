"""Adaptateur CinetPay (API REST v1 : api.cinetpay.co / sandbox api.cinetpay.net).

Référence : SDK officiels CinetPay (github.com/cinetpay/cinetpay-python,
cinetpay-php-sdk v3.0.1 du 11/08/2026). L'ancienne API « site_id / apikey »
et son en-tête x-token HMAC ont été remplacés par cette API ; l'ancienne
documentation (docs.cinetpay.com) n'est plus en ligne. Détails et écarts :
docs/MODULE_PAIEMENTS.md, « Adaptateur CinetPay ».

- Authentification : POST /v1/oauth/login {api_key, api_password} → jeton
  Bearer, mis en cache 23 h, renouvelé une fois sur EXPIRED_TOKEN/INVALID_TOKEN.
- Initiation : POST /v1/payment → payment_url, transaction_id, notify_token.
- Notification : {notify_token, merchant_transaction_id, transaction_id}.
  Le notify_token est propre à la transaction : seule son empreinte est
  stockée (Paiement.hash_jeton_notification) et comparée à temps constant.
  Le statut éventuellement présent dans la notification est IGNORÉ.
- Vérification : GET /v1/payment/{merchant_transaction_id}, identifiants
  recoupés avec la notification, systématiquement avant toute validation.
  Un 400 ou un 404 y lève TransactionIntrouvable (cette transaction) ; tout
  autre échec lève ErreurFournisseur (le service).
- Transfert : POST /v1/transfer, vérification GET /v1/transfer/{id}.
- Remboursement : aucune API publiée → traitement manuel (tableau de bord).
"""

import hashlib
import logging
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.core.cache import cache

from .base import (
    ECHEC,
    EN_ATTENTE,
    SUCCES,
    ErreurFournisseur,
    EtatTransaction,
    FournisseurPaiement,
    MontantHorsLimites,
    Notification,
    NotificationInvalide,
    ResultatTransfert,
    SessionPaiement,
    TransactionIntrouvable,
)

logger = logging.getLogger(__name__)

URL_PRODUCTION = "https://api.cinetpay.co"
URL_SANDBOX = "https://api.cinetpay.net"
DUREE_JETON_SECONDES = 23 * 3600
CODES_JETON_EXPIRE = {1002, 1003}
#: Codes HTTP qui, sur la vérification d'un paiement, visent CETTE
#: transaction (inconnue, référence refusée) et non le service. Le format
#: exact de ces réponses reste à confirmer en sandbox.
HTTP_TRANSACTION_INTROUVABLE = {400, 404}
LONGUEUR_MAX_URL = 120

PAIEMENT_MIN, PAIEMENT_MAX = 100, 2_500_000
TRANSFERT_MIN, TRANSFERT_MAX = 100, 1_500_000

#: Paiement.Methode → moyen CinetPay présélectionné. Carte bancaire : aucun
#: moyen imposé, la page CinetPay propose la carte si elle est activée sur
#: le compte marchand.
MOYENS_CINETPAY = {
    "wave": "WAVE_CI",
    "orange_money": "OM_CI",
    "mtn_money": "MTN_CI",
    "moov_money": "MOOV_CI",
}

STATUTS_SUCCES = {"SUCCESS"}
STATUTS_ECHEC = {"FAILED", "EXPIRED", "OTP_ERROR", "OTP_EXPIRED", "INSUFFICIENT_BALANCE",
                 "USER_NOT_FOUND", "USER_IS_BLOCKED", "NOT_ALLOWED"}


def _statut(valeur):
    if valeur in STATUTS_SUCCES:
        return SUCCES
    if valeur in STATUTS_ECHEC:
        return ECHEC
    return EN_ATTENTE  # INITIATED, PENDING, ou statut inconnu : on n'agit pas


def _montant(valeur):
    if valeur in (None, ""):
        return None
    try:
        montant = Decimal(str(valeur))
    except InvalidOperation:
        return None
    return int(montant) if montant == montant.to_integral_value() else None


class FournisseurCinetPay(FournisseurPaiement):
    code = "cinetpay"

    # --- Configuration et HTTP ---------------------------------------------

    def _identifiants(self):
        cle, mot_de_passe = settings.CINETPAY_API_KEY, settings.CINETPAY_API_PASSWORD
        if not cle or not mot_de_passe:
            raise ErreurFournisseur("CinetPay n'est pas configuré (CINETPAY_API_KEY / CINETPAY_API_PASSWORD).")
        return cle, mot_de_passe

    def _url_api(self):
        if settings.CINETPAY_API_URL:
            return settings.CINETPAY_API_URL.rstrip("/")
        cle, _ = self._identifiants()
        return URL_SANDBOX if cle.startswith("sk_test_") else URL_PRODUCTION

    def _cle_cache(self):
        cle, _ = self._identifiants()
        return f"cinetpay:jeton:{hashlib.sha256(cle.encode()).hexdigest()[:16]}"

    def _jeton(self, renouveler=False):
        if not renouveler:
            jeton = cache.get(self._cle_cache())
            if jeton:
                return jeton
        cle, mot_de_passe = self._identifiants()
        donnees = self._appel("POST", "/v1/oauth/login", {"api_key": cle, "api_password": mot_de_passe}, jeton=None)
        jeton = donnees.get("access_token")
        if not jeton:
            raise ErreurFournisseur("Authentification CinetPay refusée.")
        cache.set(self._cle_cache(), jeton, DUREE_JETON_SECONDES)
        return jeton

    def _appel(self, methode, chemin, corps=None, jeton=None, transaction_visee=False):
        """transaction_visee : un 400 ou un 404 concerne la transaction
        demandée (TransactionIntrouvable) ; sinon, et pour tout autre échec,
        ErreurFournisseur (fournisseur injoignable ou opération refusée)."""
        entetes = {"Accept": "application/json"}
        if jeton:
            entetes["Authorization"] = f"Bearer {jeton}"
        try:
            reponse = requests.request(
                methode, f"{self._url_api()}{chemin}", json=corps, headers=entetes,
                timeout=settings.CINETPAY_TIMEOUT,
            )
            donnees = reponse.json()
        except requests.RequestException as erreur:
            logger.error("CinetPay %s %s injoignable : %s", methode, chemin, type(erreur).__name__)
            raise ErreurFournisseur("CinetPay est injoignable.") from None
        except ValueError:
            raise ErreurFournisseur("Réponse CinetPay illisible.") from None
        if not isinstance(donnees, dict):
            raise ErreurFournisseur("Réponse CinetPay illisible.")
        if reponse.status_code >= 400:
            code = donnees.get("code")
            if code in CODES_JETON_EXPIRE or donnees.get("status") in ("EXPIRED_TOKEN", "INVALID_TOKEN"):
                raise _JetonExpire()
            # Jamais les identifiants dans les journaux : seul le code métier.
            logger.error("CinetPay %s %s refusé : %s %s", methode, chemin, code, donnees.get("status"))
            motif = donnees.get("status") or code
            if transaction_visee and reponse.status_code in HTTP_TRANSACTION_INTROUVABLE:
                raise TransactionIntrouvable(
                    f"CinetPay ne renvoie pas cette transaction ({reponse.status_code} {motif})."
                )
            raise ErreurFournisseur(f"CinetPay a refusé l'opération ({motif}).")
        return donnees

    def _requete(self, methode, chemin, corps=None, transaction_visee=False):
        """Appel authentifié, avec un seul renouvellement du jeton. Le 4xx
        d'une authentification n'est jamais « propre à la transaction »."""
        try:
            return self._appel(methode, chemin, corps, jeton=self._jeton(), transaction_visee=transaction_visee)
        except _JetonExpire:
            return self._appel(methode, chemin, corps, jeton=self._jeton(renouveler=True),
                               transaction_visee=transaction_visee)

    def _url(self, url):
        if len(url) > LONGUEUR_MAX_URL:
            raise ErreurFournisseur(f"URL de plus de {LONGUEUR_MAX_URL} caractères refusée par CinetPay : {url}")
        return url

    # --- Encaissement -------------------------------------------------------

    def initier(self, paiement):
        montant = int(paiement.montant)
        if not PAIEMENT_MIN <= montant <= PAIEMENT_MAX:
            raise MontantHorsLimites(
                f"CinetPay accepte des paiements de {PAIEMENT_MIN} à {PAIEMENT_MAX} FCFA."
            )
        client = paiement.client
        retour = f"{settings.FRONTEND_BASE_URL}/paiement/retour?reference={paiement.reference}"
        corps = {
            "currency": paiement.devise,
            "merchant_transaction_id": paiement.reference,
            "amount": montant,
            "lang": "fr",
            "designation": f"Commande ANITCHE {paiement.reference}",
            "client_email": client.email,
            "client_first_name": (client.prenom or "Client")[:255].ljust(2, "-"),
            "client_last_name": (client.nom or "ANITCHE")[:255].ljust(2, "-"),
            "success_url": self._url(retour),
            "failed_url": self._url(retour),
            "notify_url": self._url(f"{settings.BACKEND_BASE_URL}/api/paiements/webhook/{self.code}/"),
        }
        moyen = MOYENS_CINETPAY.get(paiement.methode)
        if moyen:
            corps["payment_method"] = moyen
        donnees = self._requete("POST", "/v1/payment", corps)
        if not donnees.get("payment_url") or not donnees.get("notify_token"):
            logger.error("Initiation CinetPay incomplète pour %s : %s", paiement.reference, donnees.get("status"))
            raise ErreurFournisseur("CinetPay n'a pas ouvert la transaction.")
        return SessionPaiement(
            url_paiement=donnees["payment_url"],
            identifiant_externe=str(donnees.get("transaction_id") or ""),
            jeton_notification=donnees["notify_token"],
        )

    def _lire(self, request, prefixe):
        donnees = request.data if hasattr(request.data, "get") else {}
        jeton = str(donnees.get("notify_token") or "")
        reference = str(donnees.get("merchant_transaction_id") or "")
        identifiant = str(donnees.get("transaction_id") or "")
        if not jeton or not reference or not identifiant:
            raise NotificationInvalide("notify_token, merchant_transaction_id ou transaction_id manquant.")
        return Notification(
            reference=reference,
            identifiant_externe=identifiant,
            cle_idempotence=f"{prefixe}:{identifiant}",
            jeton=jeton,
            # Le statut éventuellement transmis n'est jamais utilisé.
            donnees={"merchant_transaction_id": reference, "transaction_id": identifiant},
        )

    def lire_notification(self, request):
        return self._lire(request, "paiement")

    def _etat_canonique(self, chemin, reference, notification, transaction_visee=False):
        donnees = self._requete("GET", chemin, transaction_visee=transaction_visee)
        if str(donnees.get("merchant_transaction_id") or "") != reference:
            raise NotificationInvalide("La transaction CinetPay ne correspond pas à la référence attendue.")
        identifiant = str(donnees.get("transaction_id") or "")
        if notification is not None and identifiant != notification.identifiant_externe:
            raise NotificationInvalide("La transaction CinetPay ne correspond pas à la notification reçue.")
        return EtatTransaction(
            statut=_statut(donnees.get("status")),
            identifiant_externe=identifiant,
            montant=_montant(donnees.get("amount")),
            devise=donnees.get("currency") or None,
        )

    def verifier_transaction(self, paiement, notification=None):
        return self._etat_canonique(f"/v1/payment/{paiement.reference}", paiement.reference, notification,
                                    transaction_visee=True)

    # --- Reversements -------------------------------------------------------

    def transferer(self, reversement, telephone, operateur):
        montant = int(reversement.montant_a_verser)
        if not TRANSFERT_MIN <= montant <= TRANSFERT_MAX:
            raise MontantHorsLimites(
                f"CinetPay accepte des transferts de {TRANSFERT_MIN} à {TRANSFERT_MAX} FCFA."
            )
        moyen = MOYENS_CINETPAY.get(operateur)
        if not moyen:
            raise ErreurFournisseur(f"Opérateur de transfert inconnu : {operateur}.")
        donnees = self._requete("POST", "/v1/transfer", {
            "currency": "XOF",
            "merchant_transaction_id": reversement.reference,
            "phone_number": telephone,
            "amount": montant,
            "payment_method": moyen,
            "reason": f"Reversement ANITCHE {reversement.reference}",
            "notify_url": self._url(f"{settings.BACKEND_BASE_URL}/api/paiements/webhook/{self.code}/transfert/"),
        })
        return ResultatTransfert(
            statut=_statut(donnees.get("status")),
            identifiant_externe=str(donnees.get("transaction_id") or ""),
            jeton_notification=str(donnees.get("notify_token") or ""),
        )

    def lire_notification_transfert(self, request):
        return self._lire(request, "transfert")

    def verifier_transfert(self, reversement, notification=None):
        return self._etat_canonique(f"/v1/transfer/{reversement.reference}", reversement.reference, notification)


class _JetonExpire(ErreurFournisseur):
    """Jeton refusé : _requete le renouvelle une fois. Refusé de nouveau,
    c'est une panne du fournisseur, traitée comme toute ErreurFournisseur
    (réconciliation interrompue, webhook en 503)."""
