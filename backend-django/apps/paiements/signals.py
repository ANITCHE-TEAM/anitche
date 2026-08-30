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
    from apps.livraison.models import Livraison

    commandes_a_traiter = []

    if paiement.commande:
        commandes_a_traiter.append(paiement.commande)
    elif paiement.groupe_commande:
        commandes_a_traiter.extend(paiement.groupe_commande.commandes.all())

    adresse = adresse_livraison or paiement.adresse_livraison or "Abidjan, Côte d'Ivoire"

    with transaction.atomic():
        for commande in commandes_a_traiter:
            # 1. Mise à jour du statut de la commande si elle était encore à l'état CREEE
            if commande.status == Commande.Status.CREEE:
                commande.status = Commande.Status.CONFIRMEE
                commande.save(update_fields=["status", "update_at"])
                logger.info(f"Commande {commande.numero_commande} passée à l'état CONFIRMEE suite au paiement {paiement.reference}.")

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
