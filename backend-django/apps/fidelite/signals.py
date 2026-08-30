import logging
from decimal import Decimal
from django.dispatch import receiver

logger = logging.getLogger(__name__)

# Écouteur sur la validation d'un paiement pour créditer les points de fidélité
try:
    from apps.paiements.signals import paiement_valide
    from .models import CompteFidelite

    @receiver(paiement_valide)
    def crediter_points_apres_achat(sender, paiement, client, **kwargs):
        """Crédite automatiquement 1 point de fidélité par tranche de 1000 FCFA dépensée."""
        if not client or paiement.montant <= Decimal("0.00"):
            return

        points_gagnes = int(paiement.montant // Decimal("1000.00"))
        if points_gagnes <= 0:
            return

        compte, _ = CompteFidelite.objects.get_or_create(utilisateur=client)
        compte.crediter_points(
            points=points_gagnes,
            description=f"Gain de fidélité sur paiement {paiement.reference}",
            reference_externe=paiement.reference,
        )
        logger.info(f"{points_gagnes} points de fidélité crédités à {client.email} suite au paiement {paiement.reference}.")

        # Notification client
        try:
            from apps.notifications.services import ServiceNotification
            from apps.notifications.models import Notification

            ServiceNotification.notifier_utilisateur(
                destinataire=client,
                titre="🎉 Points de fidélité gagnés !",
                message=f"Félicitations ! Vous venez de gagner {points_gagnes} points de fidélité (Nouveau solde : {compte.solde_points} pts).",
                type_notification=Notification.TypeNotification.SYSTEME,
                lien_redirection="/fidelite/mon-compte",
                metadata={"points_gagnes": points_gagnes, "nouveau_solde": compte.solde_points},
            )
        except Exception as e:
            logger.warning(f"Erreur lors de la notification de fidélité: {e}")

except ImportError:
    logger.warning("Signal 'paiement_valide' indisponible dans apps.fidelite.")
