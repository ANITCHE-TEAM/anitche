import logging
from django.dispatch import receiver

from .models import Notification
from .services import ServiceNotification

logger = logging.getLogger(__name__)


# 1. Écouteur pour la validation d'un paiement
try:
    from apps.paiements.signals import paiement_valide

    @receiver(paiement_valide)
    def notifier_apres_paiement(sender, paiement, client, **kwargs):
        """Notifie le client de la bonne réception du paiement et les vendeurs pour la préparation."""
        # A. Notification au client
        ServiceNotification.notifier_utilisateur(
            destinataire=client,
            titre="Paiement confirmé",
            message=(
                f"Votre paiement de {paiement.montant} {paiement.devise} (Réf: {paiement.reference}) "
                f"a été validé avec succès. Vos articles sont en cours de préparation."
            ),
            type_notification=Notification.TypeNotification.PAIEMENT,
            lien_redirection=f"/commandes/{paiement.commande_id or ''}",
            metadata={"paiement_id": str(paiement.id), "reference": paiement.reference},
        )

        # B. Notifications aux boutiques / vendeurs concernés
        commandes = []
        if paiement.commande:
            commandes.append(paiement.commande)
        elif paiement.groupe_commande:
            commandes.extend(paiement.groupe_commande.commandes.select_related("boutique__proprietaire").all())

        for c in commandes:
            vendeur = getattr(getattr(c, "boutique", None), "proprietaire", None)
            if vendeur:
                ServiceNotification.notifier_utilisateur(
                    destinataire=vendeur,
                    titre=f"Nouvelle commande à préparer ({c.numero_commande})",
                    message=(
                        f"La commande {c.numero_commande} pour un montant de {c.montant_total} FCFA "
                        f"a été payée. Vous pouvez débuter sa préparation."
                    ),
                    type_notification=Notification.TypeNotification.COMMANDE,
                    lien_redirection=f"/vendeur/commandes/{c.id}",
                    metadata={"commande_id": str(c.id), "numero_commande": c.numero_commande},
                )

except ImportError:
    logger.warning("Signal 'paiement_valide' non disponible dans apps.notifications.")


# 2. Écouteur pour le changement de statut d'une livraison
try:
    from apps.livraison.signals import livraison_status_change

    @receiver(livraison_status_change)
    def notifier_changement_statut_livraison(sender, livraison, ancien_status, nouveau_status, **kwargs):
        """Notifie le client à chaque étape d'avancement de sa livraison."""
        client = getattr(getattr(livraison, "commande", None), "client", None)
        if not client:
            return

        statuts_messages = {
            "expediee": "Votre colis a quitté l'entrepôt du vendeur et a été expédié.",
            "en_cours": "Votre colis est actuellement en cours de livraison vers votre adresse.",
            "livree": "Votre colis a été livré ! Merci d'avoir choisi ANITCHE.",
            "echouee": "La livraison de votre colis a rencontré un problème. Notre service client prend le relais.",
        }

        message_corps = statuts_messages.get(
            nouveau_status,
            f"Le statut de votre livraison est désormais : {livraison.get_status_display()}."
        )

        ServiceNotification.notifier_utilisateur(
            destinataire=client,
            titre=f"Livraison {livraison.commande.numero_commande} : {livraison.get_status_display()}",
            message=message_corps,
            type_notification=Notification.TypeNotification.LIVRAISON,
            lien_redirection=f"/livraisons/{livraison.id}",
            metadata={
                "livraison_id": str(livraison.id),
                "commande_numero": livraison.commande.numero_commande,
                "nouveau_status": nouveau_status,
            },
        )

except ImportError:
    logger.warning("Signal 'livraison_status_change' non disponible dans apps.notifications.")
