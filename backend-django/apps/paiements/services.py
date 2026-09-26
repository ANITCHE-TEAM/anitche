import logging
import uuid
from decimal import Decimal
from django.db import IntegrityError
from django.db import transaction
from django.db.models import Q
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
        adresse_livraison = validated_data["_adresse_livraison"]

        commande = cible_objet if type_cible == "commande" else None
        groupe_commande = cible_objet if type_cible == "groupe" else None

        metadata = {
            "canal": "api_web",
            "telephone_client": telephone,
            # Commandes réellement payées par cette transaction (celles d'un
            # groupe déjà annulées en sont exclues).
            "commandes_couvertes": [str(c.pk) for c in validated_data["_commandes"]],
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
                adresse_livraison=adresse_livraison,
                metadata=metadata,
            )

            # Dans le cas spécifique du paiement à la livraison (Cash on Delivery),
            # la commande passe directement en confirmation/préparation sans attendre de transaction électronique.
            if methode == Paiement.Methode.ESPECE_LIVRAISON:
                from apps.commandes.services import confirmer_commande
                from apps.livraison.models import Livraison

                for c in validated_data["_commandes"]:
                    confirmer_commande(c)
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
    def traiter_webhook(fournisseur, evenement_id, reference, statut, transaction_id_externe=None, montant_recu=None, payload=None, metadata=None):
        """Traite de façon idempotente les notifications des passerelles de paiement."""
        payload = payload or {}
        metadata = metadata or {}

        # 1. Vérification d'idempotence (get_or_create est atomique côté DB
        # grâce à la contrainte unique (fournisseur, evenement_id) : deux
        # requêtes concurrentes avec le même evenement_id ne peuvent jamais
        # lever d'IntegrityError ici, Django gère la course en interne).
        journal, cree = JournalWebhook.objects.get_or_create(
            fournisseur=fournisseur,
            evenement_id=evenement_id,
            defaults={
                "payload": payload,
                "statut_traitement": JournalWebhook.StatutTraitement.TRAITE,
            },
        )

        if not cree and journal.statut_traitement == JournalWebhook.StatutTraitement.TRAITE:
            logger.info(f"Webhook {fournisseur}:{evenement_id} déjà traité. Ignoré pour idempotence.")
            return True, "Événement déjà traité."

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
                # Le montant confirmé par la passerelle doit correspondre au
                # montant attendu en base : une passerelle compromise, mal
                # intégrée, ou un rejeu avec un montant modifié ne doit
                # jamais suffire à valider un paiement pour un montant
                # inférieur (ou différent) de celui dû (A08:2025).
                if montant_recu is not None and montant_recu != paiement.montant:
                    journal.statut_traitement = JournalWebhook.StatutTraitement.ERREUR
                    journal.erreur = (
                        f"Montant reçu ({montant_recu}) différent du montant attendu "
                        f"({paiement.montant}) pour le paiement {paiement.reference}."
                    )
                    journal.save(update_fields=["statut_traitement", "erreur"])
                    logger.error(
                        f"Webhook {fournisseur}:{evenement_id} rejeté : "
                        f"écart de montant sur le paiement {paiement.reference}."
                    )
                    return False, journal.erreur

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

# =====================================================================
# REMBOURSEMENTS DUS (commandes annulées après encaissement)
# =====================================================================

logger_securite = logging.getLogger("securite")


def commandes_couvertes(paiement):
    """Commandes réellement payées par ce paiement."""
    from apps.commandes.models import Commande

    identifiants = (paiement.metadata or {}).get("commandes_couvertes")
    if identifiants:
        return list(Commande.objects.filter(pk__in=identifiants))
    if paiement.commande_id:
        return [paiement.commande]
    if paiement.groupe_commande_id:
        return list(paiement.groupe_commande.commandes.all())
    return []


def marquer_a_rembourser(paiement, commande, motif):
    """Passe le paiement « à rembourser » pour cette commande (une seule
    fois par commande) et alerte l'administration. Le détail des montants
    dus est gardé dans metadata.remboursements_dus ; le remboursement
    lui-même sera traité avec le module paiements."""
    with transaction.atomic():
        verrouille = Paiement.objects.select_for_update().get(pk=paiement.pk)
        dus = list(verrouille.metadata.get("remboursements_dus", []))
        if any(du["commande"] == str(commande.pk) for du in dus):
            return
        dus.append({
            "commande": str(commande.pk),
            "numero_commande": commande.numero_commande,
            "montant": str(commande.montant_total),
            "motif": motif,
        })
        verrouille.metadata = {**verrouille.metadata, "remboursements_dus": dus}
        verrouille.statut = Paiement.Statut.A_REMBOURSER
        verrouille.save(update_fields=["statut", "metadata", "date_mise_a_jour"])
    paiement.refresh_from_db(fields=["statut", "metadata", "date_mise_a_jour"])
    alerter_administration(verrouille, commande, motif)


def alerter_administration(paiement, commande, motif):
    """Journal de sécurité + notification (in-app et email) de chaque administrateur actif."""
    from apps.notifications.models import Notification
    from apps.notifications.services import ServiceNotification
    from apps.utilisateurs.models import Utilisateur
    from apps.vendeurs.permissions import ROLES_ADMINISTRATION

    message = (
        f"Paiement {paiement.reference} à rembourser : commande {commande.numero_commande} "
        f"({commande.montant_total} FCFA) — {motif}."
    )
    logger_securite.error("ALERTE ADMINISTRATION — %s", message)
    for administrateur in Utilisateur.objects.filter(role__in=ROLES_ADMINISTRATION, is_active=True):
        ServiceNotification.notifier_utilisateur(
            administrateur,
            titre="Paiement à rembourser",
            message=message,
            type_notification=Notification.TypeNotification.PAIEMENT,
            metadata={"paiement": str(paiement.pk), "commande": str(commande.pk)},
        )


def traiter_paiements_apres_annulation(commande):
    """Appelée à l'annulation d'une commande (dans sa transaction) :
    paiement encaissé → « à rembourser » ; paiement encore en attente dont
    toutes les commandes sont annulées → annulé."""
    from apps.commandes.models import Commande

    filtre = Q(commande=commande)
    if commande.groupe_id:
        filtre |= Q(groupe_commande_id=commande.groupe_id)
    en_cours = (Paiement.Statut.VALIDE, Paiement.Statut.A_REMBOURSER, Paiement.Statut.EN_ATTENTE)
    for paiement in Paiement.objects.filter(filtre, statut__in=en_cours):
        couvertes = commandes_couvertes(paiement)
        if commande.pk not in {c.pk for c in couvertes}:
            continue
        if paiement.statut == Paiement.Statut.EN_ATTENTE:
            if all(c.status == Commande.Status.ANNULEE for c in couvertes):
                paiement.marquer_annule(motif="commande annulée")
        else:
            marquer_a_rembourser(paiement, commande, f"commande annulée ({commande.get_motif_annulation_display()})")
