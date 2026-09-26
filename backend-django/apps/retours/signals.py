import logging
from django.dispatch import Signal, receiver

logger = logging.getLogger(__name__)

# Signal émis lors d'un changement de statut d'une demande de retour
retour_status_change = Signal()


@receiver(retour_status_change)
def notifier_changement_statut_retour(sender, demande_retour, ancien_statut, nouveau_statut, **kwargs):
    """Notifie le client et le vendeur lors des étapes d'un retour."""
    try:
        from apps.notifications.services import ServiceNotification
        from apps.notifications.models import Notification

        statuts_libelles = {
            "approuve": "Votre demande de retour a été approuvée par le vendeur.",
            "rejete": "Votre demande de retour a été rejetée.",
            "en_transit": "Votre colis retour est en transit.",
            "receptionne": "Votre colis retour a bien été réceptionné par la boutique.",
            "rembourse": "Le remboursement de votre retour a été initié.",
            "cloture": "Le dossier de retour a été clôturé.",
        }

        msg = statuts_libelles.get(
            nouveau_statut,
            f"Le statut de votre demande de retour {demande_retour.numero_retour} est désormais : {demande_retour.get_statut_display()}."
        )

        ServiceNotification.notifier_utilisateur(
            destinataire=demande_retour.client,
            titre=f"Retour {demande_retour.numero_retour} : {demande_retour.get_statut_display()}",
            message=msg,
            type_notification=Notification.TypeNotification.COMMANDE,
            lien_redirection=f"/retours/{demande_retour.id}",
            metadata={"retour_id": str(demande_retour.id), "statut": nouveau_statut},
        )

    except Exception as e:
        logger.warning(f"Erreur lors de la notification de retour: {e}")
