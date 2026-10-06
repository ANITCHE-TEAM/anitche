import logging

from django.dispatch import receiver

from apps.retours.signals import retour_status_change

logger = logging.getLogger(__name__)

# Aucun point n'est crédité au paiement (une commande payée puis annulée
# garderait ses points) : ils naissent à la livraison, « en attente »
# pendant le délai de rétractation (apps.fidelite.services).


@receiver(retour_status_change)
def ajuster_points_apres_retour(sender, demande_retour, ancien_statut, nouveau_statut, **kwargs):
    """Retour remboursé : gain en attente recalculé, ou reprise des points
    déjà crédités (plafonnée au solde). Ne fait jamais échouer le retour,
    déjà enregistré (signal émis après le commit)."""
    if nouveau_statut != "rembourse":
        return
    from .services import appliquer_retour_rembourse

    try:
        appliquer_retour_rembourse(demande_retour)
    except Exception:
        logger.exception(
            "Ajustement des points impossible après le retour %s.", getattr(demande_retour, "numero_retour", "?"),
        )
