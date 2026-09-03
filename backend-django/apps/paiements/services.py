import logging
import uuid
from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from .models import Paiement, JournalWebhook

logger = logging.getLogger(__name__)


class ServicePaiement:
    """Service d'orchestration des paiements et intégration des passerelles."""

    @staticmethod
    def initier_paiement(client, validated_data):
        """Crée une transaction de paiement et initialise la session auprès de la passerelle."""
        type_cible = validated_data.get("_type_cible")
        cible_objet = validated_data.get("_cible_objet")
        montant = validated_data.get("_montant")
        methode = validated_data.get("methode")
        telephone = validated_data.get("telephone", "")
        adresse_livraison = validated_data.get("adresse_livraison", "")

        commande = cible_objet if type_cible == "commande" else None
        groupe_commande = cible_objet if type_cible == "groupe" else None

        metadata = {
            "canal": "api_web",
            "telephone_client": telephone,
        }

        # Génération d'une URL de redirection simulée / passerelle
        identifiant_passerelle = f"ext_{uuid.uuid4().hex[:12]}"
        url_paiement = None

        if methode == Paiement.Methode.WAVE:
            url_paiement = f"https://pay.wave.com/c/cos-anitche-{uuid.uuid4().hex[:8]}"
            metadata["type_flux"] = "redirection_qr"
        elif methode in (Paiement.Methode.ORANGE_MONEY, Paiement.Methode.MTN_MONEY, Paiement.Methode.MOOV_MONEY):
            metadata["type_flux"] = "ussd_push"
            metadata["telephone_debite"] = telephone
        elif methode == Paiement.Methode.CARTE_BANCAIRE:
            url_paiement = f"https://checkout.anitche.ci/card/{uuid.uuid4().hex[:10]}"
            metadata["type_flux"] = "carte_3ds"
        elif methode == Paiement.Methode.ESPECE_LIVRAISON:
            metadata["type_flux"] = "paiement_a_la_livraison"

        with transaction.atomic():
            paiement = Paiement.objects.create(
                client=client,
                commande=commande,
                groupe_commande=groupe_commande,
                methode=methode,
                montant=montant,
                statut=Paiement.Statut.EN_ATTENTE,
                transaction_id_externe=identifiant_passerelle,
                url_paiement=url_paiement,
                adresse_livraison=adresse_livraison or "Abidjan, Côte d'Ivoire",
                metadata=metadata,
            )

            # Dans le cas spécifique du paiement à la livraison (Cash on Delivery),
            # la commande passe directement en confirmation/préparation sans attendre de transaction électronique.
            if methode == Paiement.Methode.ESPECE_LIVRAISON:
                from apps.commandes.models import Commande
                from apps.livraison.models import Livraison

                commandes_a_confirmer = [commande] if commande else list(groupe_commande.commandes.all())
                for c in commandes_a_confirmer:
                    if c.status == Commande.Status.CREEE:
                        c.status = Commande.Status.CONFIRMEE
                        c.save(update_fields=["status", "update_at"])
                    Livraison.objects.get_or_create(
                        commande=c,
                        defaults={
                            "status": Livraison.Status.EN_ATTENTE,
                            "adresse_livraison": paiement.adresse_livraison,
                        },
                    )

        logger.info(f"Paiement {paiement.reference} initialisé pour {client.email} via {methode} ({montant} FCFA).")
        return paiement

    @staticmethod
    def traiter_webhook(fournisseur, evenement_id, reference, statut, transaction_id_externe=None, payload=None, metadata=None):
        """Traite de façon idempotente les notifications des passerelles de paiement."""
        payload = payload or {}
        metadata = metadata or {}

        # 1. Vérification d'idempotence
        journal = JournalWebhook.objects.filter(
            fournisseur=fournisseur,
            evenement_id=evenement_id,
        ).first()

        if journal and journal.statut_traitement == JournalWebhook.StatutTraitement.TRAITE:
            logger.info(f"Webhook {fournisseur}:{evenement_id} déjà traité. Ignoré pour idempotence.")
            return True, "Événement déjà traité."

        if not journal:
            journal = JournalWebhook.objects.create(
                fournisseur=fournisseur,
                evenement_id=evenement_id,
                payload=payload,
                statut_traitement=JournalWebhook.StatutTraitement.TRAITE,
            )

        # 2. Recherche du Paiement
        paiement = Paiement.objects.filter(reference=reference).first()
        if not paiement:
            # Recherche alternative par transaction externe
            if transaction_id_externe:
                paiement = Paiement.objects.filter(transaction_id_externe=transaction_id_externe).first()

        if not paiement:
            journal.statut_traitement = JournalWebhook.StatutTraitement.ERREUR
            journal.erreur = f"Paiement de référence '{reference}' introuvable."
            journal.save(update_fields=["statut_traitement", "erreur"])
            return False, journal.erreur

        # 3. Application de l'état
        try:
            if statut == "succes":
                paiement.valider(
                    transaction_id_externe=transaction_id_externe or paiement.transaction_id_externe,
                    donnees_supplementaires=metadata,
                )
            elif statut == "echec":
                paiement.marquer_echoue(
                    motif=metadata.get("motif", "Échec notifié par la passerelle"),
                    donnees_supplementaires=metadata,
                )
            elif statut == "annule":
                paiement.marquer_annule(
                    motif=metadata.get("motif", "Annulation notifiée par la passerelle"),
                )

            journal.statut_traitement = JournalWebhook.StatutTraitement.TRAITE
            journal.save(update_fields=["statut_traitement"])
            return True, f"Paiement {paiement.reference} mis à jour avec le statut '{statut}'."

        except Exception as e:
            logger.exception(f"Erreur lors du traitement du webhook {fournisseur}:{evenement_id}")
            journal.statut_traitement = JournalWebhook.StatutTraitement.ERREUR
            journal.erreur = str(e)
            journal.save(update_fields=["statut_traitement", "erreur"])
            return False, str(e)
