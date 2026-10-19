import logging
from django.dispatch import Signal, receiver
from django.utils import timezone

from apps.notifications import liens

logger = logging.getLogger(__name__)

# Émis par apps.retours.services (traiter, appliquer_echeance), après le
# commit, à chaque changement de statut. Arguments : demande_retour,
# ancien_statut, nouveau_statut, action, par_client (le client a fait
# l'action), source (RetourHistorique.Source), commentaire (motif de
# l'étape). Écouteurs : notifications (ci-dessous), fidélité.
retour_status_change = Signal()

MESSAGES_CLIENT = {
    "approuve": "Votre demande de retour a été approuvée par le vendeur. Vous pouvez expédier le colis.",
    "rejete": "Votre demande de retour a été rejetée.",
    "en_transit": "Votre colis retour est en transit.",
    "receptionne": "Votre colis retour a bien été réceptionné par la boutique.",
    "rembourse": "Le remboursement de votre retour a été initié.",
    "cloture": "Le dossier de retour a été clôturé.",
}

#: Actions du client : c'est le vendeur qu'il faut prévenir.
MESSAGES_VENDEUR = {
    "en_transit": "Le client a expédié le colis retour {numero}.",
    "annuler": "Le client a annulé sa demande de retour {numero}.",
}

#: Qui examine le dossier après une contestation, selon le statut d'arrivée.
INSTANCES = {"en_moderation": "la modération", "en_arbitrage": "l'administration"}


def date_lisible(valeur):
    """Date et heure locales (Africa/Abidjan) d'une échéance."""
    return timezone.localtime(valeur).strftime("%d/%m/%Y à %H:%M") if valeur else "—"


def _notifier(destinataire, titre, message, demande, lien, **metadata):
    """Une notification qui échoue ne remet jamais en cause le retour
    lui-même (déjà enregistré) : elle est journalisée."""
    from apps.notifications.models import Notification
    from apps.notifications.services import ServiceNotification

    try:
        ServiceNotification.notifier_utilisateur(
            destinataire=destinataire,
            titre=titre,
            message=message,
            type_notification=Notification.TypeNotification.COMMANDE,
            lien_redirection=lien,
            metadata={"retour_id": str(demande.id), **metadata},
        )
    except Exception:
        logger.exception("Notification du retour %s impossible.", demande.numero_retour)


def _notifier_groupe(groupe, titre, message, demande, lien):
    """Modérateurs (groupe "moderation") ou administrateurs actifs, in-app."""
    from apps.notifications.models import Notification
    from apps.notifications.services import ServiceNotification

    envoyer = ServiceNotification.notifier_moderation if groupe == "moderation" else (
        ServiceNotification.notifier_administration
    )
    try:
        envoyer(titre=titre, message=message, type_notification=Notification.TypeNotification.COMMANDE,
                lien_redirection=lien, metadata={"retour_id": str(demande.id)})
    except Exception:
        logger.exception("Notification du retour %s impossible.", demande.numero_retour)


def notifier_nouvelle_demande(demande):
    """Le vendeur est prévenu de chaque nouvelle demande et de son échéance ;
    le client reçoit un accusé de réception."""
    echeance = date_lisible(demande.date_limite_vendeur)
    _notifier(
        demande.boutique.proprietaire,
        titre=f"Nouvelle demande de retour ({demande.numero_retour})",
        message=(
            f"Un client demande le retour d'articles de la commande {demande.commande.numero_commande} "
            f"({demande.get_motif_display()}). Répondez depuis votre espace retours avant le {echeance} : "
            "passé ce délai, la demande est transmise à la modération."
        ),
        demande=demande,
        lien=liens.lien_retour_vendeur(demande),
    )
    _notifier(
        demande.client,
        titre=f"Demande de retour {demande.numero_retour} enregistrée",
        message=f"Votre demande a été transmise au vendeur, qui a jusqu'au {echeance} pour y répondre.",
        demande=demande,
        lien=liens.lien_retour_client(demande),
    )


def notifier_relance(demande):
    """Rappel au vendeur avant son échéance, ou alerte de l'administration
    pour un dossier qui attend une décision (apps.retours.services.relancer)."""
    try:
        if demande.statut == "demande":
            _notifier(
                demande.boutique.proprietaire,
                titre=f"Rappel : retour {demande.numero_retour} sans réponse",
                message=(
                    f"La demande de retour {demande.numero_retour} attend votre réponse jusqu'au "
                    f"{date_lisible(demande.date_limite_vendeur)}. Passé ce délai, elle est transmise à la modération."
                ),
                demande=demande,
                lien=liens.lien_retour_vendeur(demande),
            )
        else:
            instance = "de la modération" if demande.statut == "en_moderation" else "de l'arbitrage"
            logger.warning("Retour %s en attente d'une décision %s.", demande.numero_retour, instance)
            _notifier_groupe(
                "administration",
                titre="Retour en attente de décision",
                message=f"Le retour {demande.numero_retour} attend une décision {instance} depuis plus de 48 h.",
                demande=demande,
                lien=liens.lien_retour_administration(demande),
            )
    except Exception:
        logger.exception("Relance du retour %s impossible.", getattr(demande, "numero_retour", "?"))


def notifier_remboursement_impossible(demande, raison):
    """Alerte de l'administration : un retour n'a pas pu être remboursé
    (aucun paiement encaissé, montant invalide). Le retour reste réceptionné."""
    _notifier_groupe(
        "administration",
        f"Retour {demande.numero_retour} non remboursable",
        f"Le remboursement du retour {demande.numero_retour} a été refusé : {raison}",
        demande,
        liens.lien_retour_administration(demande),
    )


@receiver(retour_status_change)
def notifier_changement_statut_retour(sender, demande_retour, ancien_statut, nouveau_statut, action=None,
                                       par_client=False, source=None, commentaire="", **kwargs):
    """Prévient les parties concernées par l'étape : client, vendeur, et la
    modération ou l'administration quand un dossier leur arrive."""
    try:
        _prevenir(demande_retour, ancien_statut, nouveau_statut, action, par_client, source, commentaire)
    except Exception:
        # Le changement de statut est déjà enregistré (signal émis après le
        # commit) : une notification impossible ne doit pas produire une 500.
        logger.exception("Notifications du retour %s impossibles.", getattr(demande_retour, "numero_retour", "?"))


def _prevenir(demande, ancien_statut, nouveau_statut, action, par_client, source, commentaire):
    numero = demande.numero_retour
    titre = f"Retour {numero} : {demande.get_statut_display()}"

    def client(message):
        _notifier(demande.client, titre=titre, message=message, demande=demande,
                  lien=liens.lien_retour_client(demande), statut=nouveau_statut)

    def vendeur(message):
        _notifier(demande.boutique.proprietaire, titre=titre, message=message, demande=demande,
                  lien=liens.lien_retour_vendeur(demande), statut=nouveau_statut)

    def moderation(titre_groupe, message):
        _notifier_groupe("moderation", titre_groupe, message, demande, liens.lien_retour_moderation(demande))

    def administration(titre_groupe, message):
        _notifier_groupe("administration", titre_groupe, message, demande, liens.lien_retour_administration(demande))

    motif = f" : « {commentaire[:200]} »" if commentaire else ""

    if par_client and action in MESSAGES_VENDEUR:
        vendeur(MESSAGES_VENDEUR[action].format(numero=numero))
        if action == "annuler" and ancien_statut == "en_moderation":
            moderation("Retour annulé par le client", f"Le client a annulé le retour {numero}, en examen.")
        elif action == "annuler" and ancien_statut == "en_arbitrage":
            administration("Retour annulé par le client", f"Le client a annulé le retour {numero}, en arbitrage.")
        return

    if action == "contester":
        instance = INSTANCES[nouveau_statut]
        client(f"Votre contestation a été transmise à {instance}, qui va réexaminer votre retour.")
        vendeur(f"Le client conteste la décision sur le retour {numero} : le dossier est examiné par {instance}.")
        if nouveau_statut == "en_moderation":
            moderation("Nouveau retour à examiner", f"Le client conteste le refus du vendeur (retour {numero}){motif}.")
        else:
            administration("Retour en arbitrage",
                           f"Le client conteste le refus de la modération (retour {numero}){motif}.")
        return

    if action == "escalader":
        client("Le vendeur n'a pas répondu dans le délai : votre demande de retour est transmise à la modération.")
        vendeur(f"Délai de réponse dépassé : la demande de retour {numero} est transmise à la modération.")
        moderation("Nouveau retour à examiner", f"Le vendeur n'a pas répondu dans le délai (retour {numero}).")
        return

    if nouveau_statut in ("refuse_vendeur", "refuse_moderation"):
        qui = "Le vendeur" if nouveau_statut == "refuse_vendeur" else "La modération"
        client(
            f"{qui} a refusé votre demande de retour{motif}. Vous pouvez contester cette décision "
            f"jusqu'au {date_lisible(demande.date_limite_contestation)}."
        )
        if nouveau_statut == "refuse_moderation":
            vendeur(f"La modération a confirmé le refus du retour {numero}.")
        return

    if action == "clore_refus":
        client("Le délai de contestation est écoulé : le refus de votre retour est définitif.")
        vendeur(f"Le refus du retour {numero} est définitif : aucune contestation dans le délai.")
        return

    if action in ("approuver", "rejeter") and source in ("moderation", "administration"):
        instance = "La modération" if source == "moderation" else "L'administration"
        if nouveau_statut == "approuve":
            client(f"{instance} a approuvé votre retour : vous pouvez expédier le colis.")
            vendeur(f"{instance} a approuvé le retour {numero} : attendez le colis du client.")
        else:
            client(f"{instance} a rejeté définitivement votre retour{motif}.")
            vendeur(f"{instance} a rejeté définitivement le retour {numero}.")
        return

    client(MESSAGES_CLIENT.get(nouveau_statut, f"Statut de votre retour : {demande.get_statut_display()}."))
