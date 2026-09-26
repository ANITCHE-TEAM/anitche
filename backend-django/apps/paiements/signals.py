import logging
from django.dispatch import Signal, receiver
from django.db import transaction

logger = logging.getLogger(__name__)

# Signal émis lorsqu'un paiement est validé avec succès
# Fournit les arguments : paiement, client, adresse_livraison
paiement_valide = Signal()


@receiver(paiement_valide)
def gerer_confirmation_commandes_et_livraisons(sender, paiement, client, adresse_livraison, **kwargs):
    """Met à jour le statut des commandes associées et crée les fiches de livraison correspondantes."""
    from apps.commandes.models import Commande
    from apps.commandes.services import confirmer_commande
    from apps.livraison.models import Livraison
    from .services import commandes_couvertes, marquer_a_rembourser

    adresse = paiement.adresse_livraison

    with transaction.atomic():
        for commande in commandes_couvertes(paiement):
            # 1. creee → confirmee, par la fonction unique de transition.
            if not confirmer_commande(commande):
                if commande.status == Commande.Status.ANNULEE:
                    # Annulée pendant le traitement du paiement (course avec
                    # l'expiration) : jamais réactivée, remboursement dû.
                    marquer_a_rembourser(paiement, commande, "paiement reçu pour une commande annulée")
                    continue

            # 2. Création automatique de la fiche Livraison si elle n'existe pas encore
            livraison, cree = Livraison.objects.get_or_create(
                commande=commande,
                defaults={
                    "status": Livraison.Status.EN_ATTENTE,
                    "adresse_livraison": adresse,
                },
            )
            if cree:
                logger.info(f"Fiche Livraison créée pour la commande {commande.numero_commande}.")
