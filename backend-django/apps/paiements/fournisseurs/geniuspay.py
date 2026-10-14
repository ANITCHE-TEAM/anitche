"""Adaptateur GeniusPay (API marchand v1 : geniuspay.ci/api/v1/merchant).

Encaissement seulement. Les transferts vers les vendeurs (« cashout ») ne
sont pas implémentés : transferer lève OperationNonSupportee (contrat de
base), le versement manuel reste possible.

- Authentification : en-têtes X-API-Key et X-API-Secret sur chaque appel.
- Initiation : POST /payments → reference (stockée dans
  Paiement.transaction_id_externe), checkout_url (la documentation écrit payment_url). La référence ANITCHE du paiement part dans metadata. Aucune URL de notification dans la requête :
  elle se déclare dans le tableau de bord GeniusPay, ou par POST /webhooks
  (creer_webhook), qui renvoie une seule fois le secret de signature.
- Notification : signée, HMAC-SHA256(horodatage + "." + corps brut) avec le
  secret du webhook, en-têtes X-Webhook-Signature et X-Webhook-Timestamp,
  tolérance de 5 minutes. Elle n'est qu'un signal : son statut, son montant
  et sa devise ne sont JAMAIS utilisés. Le paiement se retrouve par la
  référence GeniusPay stockée à l'initiation, sans rien supposer de son
  format (« SANDBOX_… » en sandbox, « MTX-… » ou « TXN-… » dans la documentation).
- Vérification : GET /payments/{reference}, systématiquement avant toute
  validation. Seule sa réponse fait foi : statut, `amount` (jamais
  `net_amount` ni `fees`), `currency`, et référence, environnement et
  metadata recoupés avec le paiement.
- Environnement : celui de la notification et celui de chaque réponse
  doivent être GENIUSPAY_ENVIRONNEMENT (« sandbox » ou « live »).

Montants : le JSON est lu avec parse_float=Decimal, jamais en flottant. Un
entier, ou un décimal à partie fractionnaire nulle (10000.00), vaut
l'entier ; toute autre valeur (10000.50, texte, booléen, absente) vaut None,
et un succès est alors refusé par services.controler_montant_et_devise.

À CONFIRMER EN SANDBOX (la documentation ne le précise pas) : l'encodage de
la signature (ENCODAGE_SIGNATURE), l'usage du secret « whsec_… » tel quel
comme clé HMAC, le renvoi de metadata par le GET, et le corps de
l'événement webhook.test.

Journal de niveau INFO (visible en dev seulement) : réception d'une
notification, résultat de la signature, forme du corps, réponse du GET.
Jamais une clé, un secret, une signature ni une donnée du client.

Inactif par défaut : sans clés, sans secret de webhook ou sans
environnement (réglages GENIUSPAY_* vides), rien n'est ouvert, vérifié ni
accepté.
"""

import codecs
import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal
from urllib.parse import quote

import requests
from django.conf import settings

from .base import (
    ECHEC,
    EN_ATTENTE,
    SUCCES,
    ErreurFournisseur,
    EtatTransaction,
    FournisseurPaiement,
    MontantHorsLimites,
    Notification,
    NotificationIgnoree,
    NotificationInvalide,
    SessionPaiement,
    TransactionIntrouvable,
)

logger = logging.getLogger(__name__)
logger_securite = logging.getLogger("securite")

URL_API = "https://geniuspay.ci/api/v1/merchant"
ENVIRONNEMENTS = ("sandbox", "live")
PAIEMENT_MIN = 200
PAYS_CLIENT = "CI"
#: Clé de la référence ANITCHE du paiement dans metadata.
CLE_REFERENCE = "reference"
#: Limites des champs Paiement.transaction_id_externe et Paiement.url_paiement.
LONGUEUR_MAX_REFERENCE, LONGUEUR_MAX_URL = 150, 500

EN_TETE_SIGNATURE = "X-Webhook-Signature"
EN_TETE_HORODATAGE = "X-Webhook-Timestamp"
TOLERANCE_SECONDES = 300
#: Encodage de la signature HMAC-SHA256, non précisé par la documentation :
#: hexadécimal supposé, À CONFIRMER en sandbox (« base64 » sinon).
ENCODAGE_SIGNATURE = "hex"

EVENEMENT_ESSAI = "webhook.test"
PREFIXE_EVENEMENTS_PAIEMENT = "payment."
#: En-têtes d'information d'une notification, journalisés avant la signature.
EN_TETE_EVENEMENT = "X-Webhook-Event"
EN_TETE_ENVIRONNEMENT = "X-Webhook-Environment"

CODE_TRANSACTION_INTROUVABLE = "TRANSACTION_NOT_FOUND"
STATUTS_SUCCES = {"completed"}
STATUTS_ECHEC = {"failed", "cancelled", "expired"}
#: Aucune transition : journalisé, puis traité comme une attente.
STATUT_REMBOURSE = "refunded"


def signer(corps, secret, horodatage):
    """Signature attendue d'une notification : HMAC-SHA256 de
    « <horodatage>.<corps brut> », clé = secret du webhook."""
    empreinte = hmac.new(secret.encode(), f"{horodatage}.".encode() + corps, hashlib.sha256).digest()
    return codecs.encode(empreinte, ENCODAGE_SIGNATURE).decode().strip()


def _refuser_constante(nom):
    raise ValueError(f"Constante JSON refusée : {nom}")


def _lire_json(octets):
    """Nombres décimaux en Decimal (jamais float) ; NaN et Infinity refusés."""
    return json.loads(octets, parse_float=Decimal, parse_constant=_refuser_constante)


def _texte(valeur, longueur_max):
    return isinstance(valeur, str) and 0 < len(valeur) <= longueur_max


def _statut(valeur):
    if not isinstance(valeur, str):
        return EN_ATTENTE
    if valeur in STATUTS_SUCCES:
        return SUCCES
    if valeur in STATUTS_ECHEC:
        return ECHEC
    return EN_ATTENTE  # pending, processing, refunded ou statut inconnu : on n'agit pas


def _montant(valeur):
    """Entier, ou décimal à partie fractionnaire nulle (10000.00 → 10000).
    Tout le reste → None : 10000.50, texte, booléen, valeur absente."""
    if type(valeur) is int:  # exclut bool, sous-classe d'int
        return valeur
    if type(valeur) is Decimal and valeur.is_finite() and valeur == valeur.to_integral_value():
        return int(valeur)
    return None


def _forme_signature(signature):
    """Forme de la signature reçue, jamais sa valeur."""
    if len(signature) == 64 and all(caractere in "0123456789abcdefABCDEF" for caractere in signature):
        return "hexadécimale (64 caractères)"
    if len(signature) == 44 and signature.endswith("="):
        return "base64 probable (44 caractères)"
    return f"de forme inconnue ({len(signature)} caractères)" if signature else "absente"


def _court(valeur):
    """Valeur courte et imprimable pour le journal, sinon son type."""
    if isinstance(valeur, str) and valeur.isprintable():
        return valeur[:40]
    return f"<{type(valeur).__name__}>"


def _cles(objet):
    """Noms des champs d'un objet JSON (jamais leurs valeurs)."""
    if not isinstance(objet, dict):
        return f"<{type(objet).__name__}>"
    return ", ".join(sorted(_court(cle) for cle in objet)[:40]) or "aucun"


class FournisseurGeniusPay(FournisseurPaiement):
    code = "geniuspay"
    notification_par_identifiant_externe = True

    # --- Configuration et HTTP ---------------------------------------------

    def _entetes(self):
        cle, secret = settings.GENIUSPAY_API_KEY, settings.GENIUSPAY_API_SECRET
        if not cle or not secret:
            raise ErreurFournisseur("GeniusPay n'est pas configuré (GENIUSPAY_API_KEY / GENIUSPAY_API_SECRET).")
        return {"Accept": "application/json", "X-API-Key": cle, "X-API-Secret": secret}

    def _environnement(self):
        environnement = settings.GENIUSPAY_ENVIRONNEMENT
        if environnement not in ENVIRONNEMENTS:
            raise ErreurFournisseur("GeniusPay n'est pas configuré (GENIUSPAY_ENVIRONNEMENT : sandbox ou live).")
        return environnement

    def _url(self, chemin):
        return f"{(settings.GENIUSPAY_API_URL or URL_API).rstrip('/')}{chemin}"

    def _appel(self, methode, chemin, corps=None, transaction_visee=False):
        """→ le « data » d'une réponse réussie. transaction_visee : un 404
        TRANSACTION_NOT_FOUND concerne la transaction demandée
        (TransactionIntrouvable) ; tout autre échec — délai dépassé, 401,
        403, 429, 5xx, redirection, corps illisible — est une
        ErreurFournisseur. Jamais les clés dans les journaux : seuls le code
        HTTP et le code d'erreur de GeniusPay."""
        entetes = self._entetes()
        self._environnement()
        try:
            # Redirection jamais suivie : les clés ne partent que vers l'API configurée.
            reponse = requests.request(
                methode, self._url(chemin), json=corps, headers=entetes,
                timeout=settings.GENIUSPAY_TIMEOUT, allow_redirects=False,
            )
        except requests.RequestException as erreur:
            logger.error("GeniusPay %s %s injoignable : %s", methode, chemin, type(erreur).__name__)
            raise ErreurFournisseur("GeniusPay est injoignable.") from None
        logger.info("GeniusPay %s %s : HTTP %s.", methode, chemin, reponse.status_code)
        try:
            donnees = _lire_json(reponse.content)
        except ValueError:
            donnees = None
        if not isinstance(donnees, dict):
            logger.error("GeniusPay %s %s : réponse illisible (HTTP %s).", methode, chemin, reponse.status_code)
            raise ErreurFournisseur(f"Réponse GeniusPay illisible (HTTP {reponse.status_code}).")
        data = donnees.get("data")
        if 200 <= reponse.status_code < 300 and donnees.get("success") is True and isinstance(data, dict):
            return data
        erreur = donnees.get("error")
        code = erreur.get("code") if isinstance(erreur, dict) else None
        code = code[:60] if isinstance(code, str) else None
        logger.error("GeniusPay %s %s refusé : HTTP %s %s", methode, chemin, reponse.status_code, code)
        if transaction_visee and reponse.status_code == 404 and code == CODE_TRANSACTION_INTROUVABLE:
            raise TransactionIntrouvable(f"GeniusPay ne connaît pas cette transaction (404 {code}).")
        raise ErreurFournisseur(f"GeniusPay a refusé l'opération (HTTP {reponse.status_code}, {code}).")

    def creer_webhook(self, nom, url, evenements):
        """Déclare l'URL de notification (POST /webhooks) → le « data » de la
        réponse, qui porte le secret de signature, renvoyé cette seule fois :
        à l'appelant de le conserver sans jamais l'afficher ni le journaliser."""
        return self._appel("POST", "/webhooks", {"name": nom, "url": url, "events": list(evenements)})

    # --- Encaissement -------------------------------------------------------

    def initier(self, paiement):
        montant = int(paiement.montant)
        if montant < PAIEMENT_MIN:
            raise MontantHorsLimites(f"GeniusPay accepte des paiements à partir de {PAIEMENT_MIN} FCFA.")
        client = paiement.client
        acheteur = {
            "name": f"{client.prenom} {client.nom}".strip() or "Client ANITCHE",
            "email": client.email,
            "country": PAYS_CLIENT,
        }
        if client.telephone:
            acheteur["phone"] = client.telephone
        retour = f"{settings.FRONTEND_BASE_URL}/paiement/retour?reference={paiement.reference}"
        data = self._appel("POST", "/payments", {
            "amount": montant,
            "description": f"Commande ANITCHE {paiement.reference}",
            "customer": acheteur,
            "success_url": retour,
            "error_url": retour,
            "metadata": {CLE_REFERENCE: paiement.reference},
        })
        reference, url = data.get("reference"), data.get("checkout_url")
        if (
            data.get("environment") != self._environnement()
            or not _texte(reference, LONGUEUR_MAX_REFERENCE)
            or not _texte(url, LONGUEUR_MAX_URL)
            or not url.startswith("https://")
        ):
            logger.error("Initiation GeniusPay refusée pour %s : réponse incomplète ou d'un autre environnement.",
                         paiement.reference)
            raise ErreurFournisseur("GeniusPay n'a pas ouvert la transaction.")
        return SessionPaiement(url_paiement=url, identifiant_externe=reference)

    def lire_notification(self, request):
        # Corps BRUT : la signature porte sur les octets reçus, jamais sur un
        # JSON ré-encodé. Le JSON n'est lu qu'une fois la signature vérifiée.
        corps = request.body
        logger.info(
            "Notification GeniusPay reçue : %s octets, événement annoncé « %s », environnement annoncé « %s », "
            "signature %s.",
            len(corps), _court(request.headers.get(EN_TETE_EVENEMENT, "")),
            _court(request.headers.get(EN_TETE_ENVIRONNEMENT, "")),
            _forme_signature(request.headers.get(EN_TETE_SIGNATURE, "")),
        )
        self._verifier_signature(request, corps)
        logger.info("Notification GeniusPay : signature valide (encodage %s).", ENCODAGE_SIGNATURE)
        try:
            donnees = _lire_json(corps)
        except ValueError:
            raise NotificationInvalide("Corps JSON invalide.") from None
        if not isinstance(donnees, dict):
            raise NotificationInvalide("Corps JSON invalide.")
        evenement = donnees.get("event")
        if not _texte(evenement, 60):
            raise NotificationInvalide("Événement manquant.")
        data = donnees.get("data")
        logger.info(
            "Notification GeniusPay %s : champs [%s] ; data [%s] ; amount de type %s ; référence « %s ».",
            _court(evenement), _cles(donnees), _cles(data),
            type(data.get("amount")).__name__ if isinstance(data, dict) else "absent",
            _court(data.get("reference")) if isinstance(data, dict) else "",
        )
        if evenement == EVENEMENT_ESSAI:
            raise NotificationIgnoree(f"essai du webhook ({EVENEMENT_ESSAI})")
        try:
            environnement = self._environnement()
        except ErreurFournisseur as erreur:
            raise NotificationInvalide(str(erreur)) from None
        # L'environnement est dans data (corps réel observé en sandbox), jamais à la racine.
        if (data.get("environment") if isinstance(data, dict) else None) != environnement:
            raise NotificationInvalide(f"Notification d'un autre environnement que « {environnement} ».")
        if not evenement.startswith(PREFIXE_EVENEMENTS_PAIEMENT):
            # cashout.* (transferts, non gérés ici) et tout événement inconnu.
            raise NotificationIgnoree(f"événement {evenement} hors du périmètre des encaissements")
        reference = data.get("reference") if isinstance(data, dict) else None
        if not _texte(reference, LONGUEUR_MAX_REFERENCE):
            raise NotificationInvalide("Référence de transaction manquante.")
        identifiant = donnees.get("id")
        metadata = data.get("metadata")
        reference_anitche = metadata.get(CLE_REFERENCE) if isinstance(metadata, dict) else None
        return Notification(
            # Recoupement seulement : le paiement se retrouve par la référence GeniusPay.
            reference=reference_anitche if isinstance(reference_anitche, str) else "",
            identifiant_externe=reference,
            cle_idempotence=identifiant if _texte(identifiant, 150) else f"{evenement}:{reference}",
            # Ni statut, ni montant, ni données du client : jamais utilisés, jamais conservés.
            donnees={"event": evenement, "reference": reference, "environment": environnement},
        )

    def _verifier_signature(self, request, corps):
        secret = settings.GENIUSPAY_WEBHOOK_SECRET
        if not secret:
            raise NotificationInvalide("GeniusPay : secret du webhook non configuré (GENIUSPAY_WEBHOOK_SECRET).")
        signature = request.headers.get(EN_TETE_SIGNATURE, "")
        horodatage = request.headers.get(EN_TETE_HORODATAGE, "")
        if not signature or not (horodatage.isascii() and horodatage.isdigit() and len(horodatage) <= 12):
            raise NotificationInvalide("Signature ou horodatage manquant.")
        if abs(time.time() - int(horodatage)) > TOLERANCE_SECONDES:
            raise NotificationInvalide("Horodatage hors tolérance (rejeu refusé).")
        if not hmac.compare_digest(signature.encode(), signer(corps, secret, horodatage).encode()):
            raise NotificationInvalide("Signature invalide.")

    def authentifier_notification(self, notification, objet):
        # La signature HMAC, vérifiée à la lecture, authentifie déjà l'émetteur.
        return None

    def verifier_transaction(self, paiement, notification=None):
        reference = paiement.transaction_id_externe
        if not reference:
            raise TransactionIntrouvable("Aucune référence GeniusPay pour ce paiement (initiation inaboutie).")
        data = self._appel("GET", f"/payments/{quote(reference, safe='')}", transaction_visee=True)
        logger.info(
            "Vérification GeniusPay de %s (paiement %s) : statut « %s », amount de type %s, devise « %s », "
            "environnement « %s », metadata [%s].",
            reference, paiement.reference, _court(data.get("status")), type(data.get("amount")).__name__,
            _court(data.get("currency")), _court(data.get("environment")), _cles(data.get("metadata")),
        )
        if data.get("reference") != reference:
            raise NotificationInvalide("La transaction GeniusPay ne porte pas la référence stockée à l'initiation.")
        if notification is not None and notification.identifiant_externe != reference:
            raise NotificationInvalide("La transaction GeniusPay ne correspond pas à la notification reçue.")
        if data.get("environment") != self._environnement():
            raise NotificationInvalide("La transaction GeniusPay vient d'un autre environnement.")
        metadata = data.get("metadata")
        if not isinstance(metadata, dict) or metadata.get(CLE_REFERENCE) != paiement.reference:
            raise NotificationInvalide("La transaction GeniusPay ne désigne pas ce paiement (metadata).")
        statut, devise = data.get("status"), data.get("currency")
        if statut == STATUT_REMBOURSE:
            logger_securite.warning(
                "GeniusPay : transaction %s (paiement %s) remboursée chez le fournisseur, aucune transition appliquée.",
                reference, paiement.reference,
            )
        return EtatTransaction(
            statut=_statut(statut),
            identifiant_externe=reference,
            # `amount` (brut payé par le client), jamais `net_amount` ni `fees`.
            montant=_montant(data.get("amount")),
            devise=devise if isinstance(devise, str) and devise else None,
        )

    # --- Reversements : non implémentés (contrat de base) --------------------

    def lire_notification_transfert(self, request):
        raise NotificationInvalide("Notifications de transfert GeniusPay non prises en charge.")
