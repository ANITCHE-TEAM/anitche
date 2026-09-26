import logging
from django.dispatch import Signal, receiver

logger = logging.getLogger(__name__)

# Émis par apps.retours.services.traiter, après le commit, à chaque
# changement de statut. Arguments : demande_retour, ancien_statut,
# nouveau_statut, action, par_client (le client a fait l'action). Écouteurs : notifications (ci-dessous), fidélité.
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


def notifier_nouvelle_demande(demande):
    """Le vendeur est prévenu de chaque nouvelle demande de retour."""
    _notifier(
        demande.boutique.proprietaire,
        titre=f"Nouvelle demande de retour ({demande.numero_retour})",
        message=(
            f"Un client demande le retour d'articles de la commande {demande.commande.numero_commande} "
            f"({demande.get_motif_display()}). Répondez depuis votre espace retours."
        ),
        demande=demande,
        lien=f"/vendeur/retours/{demande.id}",
    )


@receiver(retour_status_change)
def notifier_changement_statut_retour(sender, demande_retour, ancien_statut, nouveau_statut, action=None,
                                       par_client=False, **kwargs):
    """Prévient l'autre partie : le vendeur quand le client agit, le client
    sinon ; l'administration à chaque rejet (recours possible par le support)."""
    try:
        _prevenir(demande_retour, nouveau_statut, action, par_client)
    except Exception:
        # Le changement de statut est déjà enregistré (signal émis après le
        # commit) : une notification impossible ne doit pas produire une 500.
        logger.exception("Notifications du retour %s impossibles.", getattr(demande_retour, "numero_retour", "?"))


def _prevenir(demande, nouveau_statut, action, par_client):
    from apps.notifications.models import Notification
    from apps.notifications.services import ServiceNotification

    if par_client and action in MESSAGES_VENDEUR:
        _notifier(
            demande.boutique.proprietaire,
            titre=f"Retour {demande.numero_retour} : {demande.get_statut_display()}",
            message=MESSAGES_VENDEUR[action].format(numero=demande.numero_retour),
            demande=demande,
            lien=f"/vendeur/retours/{demande.id}",
            statut=nouveau_statut,
        )
        return

    _notifier(
        demande.client,
        titre=f"Retour {demande.numero_retour} : {demande.get_statut_display()}",
        message=MESSAGES_CLIENT.get(nouveau_statut, f"Statut de votre retour : {demande.get_statut_display()}."),
        demande=demande,
        lien=f"/retours/{demande.id}",
        statut=nouveau_statut,
    )
    if nouveau_statut == "rejete":
        logger.warning("Retour %s rejeté par la boutique %s.", demande.numero_retour, demande.boutique_id)
        ServiceNotification.notifier_administration(
            titre="Retour rejeté par un vendeur",
            message=(
                f"Le retour {demande.numero_retour} (commande {demande.commande.numero_commande}) "
                f"a été rejeté : « {demande.reponse_vendeur[:200]} »."
            ),
            type_notification=Notification.TypeNotification.COMMANDE,
            lien_redirection=f"/admin/retours/{demande.id}",
            metadata={"retour_id": str(demande.id)},
        )
