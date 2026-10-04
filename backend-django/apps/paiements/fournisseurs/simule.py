"""Fournisseur simulé : développement, tests et collections Postman.

Aucun argent ne circule. La chaîne de sécurité est la même qu'en réel :
notification signée (HMAC-SHA256 sur « horodatage.corps », fenêtre de
5 minutes contre le rejeu), puis vérification de la transaction, puis
contrôles du montant, de la devise et des transitions de statut.

Signature attendue, en-tête X-Signature-Simulation :
    t=<horodatage unix>,v1=<hex HMAC-SHA256(PAIEMENT_SIMULE_SECRET, "<t>.<corps brut>")>

Corps d'une notification de paiement (JSON) :
    {"evenement_id": "...", "reference": "PAY-...", "transaction_id": "...",
     "statut": "succes" | "echec" | "en_attente", "montant": 15000, "devise": "XOF"}
Pour un transfert, « reference » est celle du reversement (REV-...).

État « côté fournisseur » (réconciliation, services.reconcilier_paiement) :
le dernier état signé reçu pour un paiement, ou celui fixé par
definir_etat_distant (tests, commande `simuler_etat_paiement` en dev), est
gardé dans le cache. verifier_transaction sans notification le renvoie ;
sans état connu, la transaction est « en attente » (le client n'a pas payé).

Refusé en production (config/settings/prod.py).
"""

import hashlib
import hmac
import json
import time
import uuid

from django.conf import settings
from django.core.cache import cache

from .base import (
    ECHEC,
    EN_ATTENTE,
    SUCCES,
    ErreurFournisseur,
    EtatTransaction,
    FournisseurPaiement,
    Notification,
    NotificationInvalide,
    ResultatTransfert,
    SessionPaiement,
)

EN_TETE_SIGNATURE = "X-Signature-Simulation"
TOLERANCE_SECONDES = 300
STATUTS = {SUCCES, ECHEC, EN_ATTENTE}
# État distant simulé : « injoignable » fait lever ErreurFournisseur à la vérification.
INJOIGNABLE = "injoignable"
DUREE_ETAT_DISTANT = 3 * 24 * 3600  # au-delà de la fenêtre de réconciliation (24 h)


def _cle_etat(reference):
    return f"paiement_simule:etat:{reference}"


def definir_etat_distant(reference, statut, montant=None, devise="XOF", identifiant_externe=""):
    """Fixe l'état que le fournisseur simulé renverra pour ce paiement
    (SUCCES, ECHEC, EN_ATTENTE ou INJOIGNABLE)."""
    if statut not in STATUTS | {INJOIGNABLE}:
        raise ValueError(f"Statut simulé inconnu : {statut}")
    cache.set(_cle_etat(reference), {
        "statut": statut, "montant": montant, "devise": devise, "identifiant_externe": identifiant_externe,
    }, DUREE_ETAT_DISTANT)


def signer(corps, secret, horodatage=None):
    """Valeur de l'en-tête de signature (utilisée par les tests)."""
    horodatage = int(time.time()) if horodatage is None else horodatage
    if isinstance(corps, str):
        corps = corps.encode()
    empreinte = hmac.new(secret.encode(), f"{horodatage}.".encode() + corps, hashlib.sha256).hexdigest()
    return f"t={horodatage},v1={empreinte}"


class FournisseurSimule(FournisseurPaiement):
    code = "simule"

    def _secret(self):
        # Secret défini seulement par dev.py et test.py ; prod.py refuse ce
        # fournisseur au démarrage.
        secret = getattr(settings, "PAIEMENT_SIMULE_SECRET", "")
        if not secret:
            raise ErreurFournisseur("Fournisseur simulé indisponible (PAIEMENT_SIMULE_SECRET vide).")
        return secret

    def initier(self, paiement):
        self._secret()
        return SessionPaiement(
            url_paiement=f"{settings.FRONTEND_BASE_URL}/paiement/simulation/{paiement.reference}",
            identifiant_externe=f"SIM-{uuid.uuid4().hex[:16].upper()}",
        )

    def lire_notification(self, request):
        try:
            secret = self._secret()
        except ErreurFournisseur as erreur:
            raise NotificationInvalide(str(erreur))
        self._verifier_signature(request, secret)
        try:
            donnees = json.loads(request.body)
        except (ValueError, UnicodeDecodeError):
            raise NotificationInvalide("Corps JSON invalide.")
        if not isinstance(donnees, dict):
            raise NotificationInvalide("Corps JSON invalide.")
        evenement_id = str(donnees.get("evenement_id") or "")
        reference = str(donnees.get("reference") or "")
        statut = donnees.get("statut")
        if not evenement_id or not reference or statut not in STATUTS:
            raise NotificationInvalide("Champs evenement_id, reference ou statut manquants ou invalides.")
        if statut == SUCCES:
            montant = donnees.get("montant")
            if isinstance(montant, str) and montant.isdigit():
                montant = int(montant)
            if not isinstance(montant, int) or isinstance(montant, bool) or not donnees.get("devise"):
                raise NotificationInvalide("Montant entier et devise obligatoires pour un succès.")
        return Notification(
            reference=reference,
            identifiant_externe=str(donnees.get("transaction_id") or ""),
            cle_idempotence=evenement_id,
            donnees=donnees,
        )

    def _verifier_signature(self, request, secret):
        valeur = request.headers.get(EN_TETE_SIGNATURE, "")
        parties = dict(morceau.split("=", 1) for morceau in valeur.split(",") if "=" in morceau)
        horodatage, signature = parties.get("t", ""), parties.get("v1", "")
        if not horodatage.isdigit() or not signature:
            raise NotificationInvalide("Signature manquante.")
        if abs(time.time() - int(horodatage)) > TOLERANCE_SECONDES:
            raise NotificationInvalide("Signature expirée (rejeu refusé).")
        attendue = signer(request.body, secret, int(horodatage)).split("v1=", 1)[1]
        if not hmac.compare_digest(signature, attendue):
            raise NotificationInvalide("Signature invalide.")

    def authentifier_notification(self, notification, objet):
        # La signature HMAC, vérifiée à la lecture, authentifie déjà l'émetteur.
        return None

    def _etat(self, notification):
        if notification is None:
            return EtatTransaction(statut=EN_ATTENTE)
        donnees = notification.donnees
        montant = donnees.get("montant")
        return EtatTransaction(
            statut=donnees["statut"],
            identifiant_externe=notification.identifiant_externe,
            montant=int(montant) if montant is not None and str(montant).isdigit() else None,
            devise=donnees.get("devise"),
        )

    def verifier_transaction(self, paiement, notification=None):
        if notification is not None:
            # Pas de serveur distant : la notification signée fait foi, et
            # devient l'état connu du « fournisseur » pour la réconciliation.
            etat = self._etat(notification)
            definir_etat_distant(paiement.reference, etat.statut, etat.montant, etat.devise or "XOF",
                                 etat.identifiant_externe)
            return etat
        connu = cache.get(_cle_etat(paiement.reference))
        if connu is None:
            return EtatTransaction(statut=EN_ATTENTE, identifiant_externe=paiement.transaction_id_externe or "")
        if connu["statut"] == INJOIGNABLE:
            raise ErreurFournisseur("Fournisseur simulé injoignable.")
        return EtatTransaction(
            statut=connu["statut"],
            identifiant_externe=connu["identifiant_externe"] or paiement.transaction_id_externe or "",
            montant=connu["montant"],
            devise=connu["devise"],
        )

    def transferer(self, reversement, telephone, operateur):
        self._secret()
        return ResultatTransfert(statut=SUCCES, identifiant_externe=f"SIMT-{uuid.uuid4().hex[:16].upper()}")

    def verifier_transfert(self, reversement, notification=None):
        return self._etat(notification)
