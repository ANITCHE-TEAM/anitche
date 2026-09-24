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


# Écouteur sur le remboursement d'un retour, pour reprendre les points de
# fidélité gagnés sur la part remboursée de l'achat.
try:
    from apps.retours.signals import retour_status_change
    from .models import CompteFidelite, TransactionFidelite
    from django.core.exceptions import ValidationError as DjangoValidationError

    @receiver(retour_status_change)
    def reprendre_points_apres_remboursement(sender, demande_retour, ancien_statut, nouveau_statut, **kwargs):
        """Reprend les points de fidélité gagnés sur la part remboursée d'un
        achat, au même taux que le gain (1 point / 1000 FCFA).

        SÉCURITÉ (A04:2025 — Insecure Design) : sans ce correctif, un client
        pouvait acheter, encaisser les points de fidélité au paiement, puis
        se faire intégralement rembourser via un retour — il gardait alors
        des points gagnés sur de l'argent qui lui a été rendu. C'était un
        moyen d'accumuler des points de fidélité (donc des coupons
        monétisables) sans aucun risque financier réel, en achetant et en
        retournant systématiquement.

        Ce correctif reste défensif de bout en bout : il ne doit JAMAIS
        faire échouer le traitement réel du remboursement du client pour
        une histoire de points (même logique que les notifications de
        apps.notifications, apps.paiements et apps.retours, déjà toutes
        enveloppées dans un try/except par précaution).
        """
        if nouveau_statut != "rembourse":
            return

        try:
            montant = demande_retour.montant_remboursement
            if not montant or montant <= Decimal("0.00"):
                return

            points_a_reprendre = int(montant // Decimal("1000.00"))
            if points_a_reprendre <= 0:
                return

            client = demande_retour.client
            if not client:
                return

            compte, _ = CompteFidelite.objects.get_or_create(utilisateur=client)

            # Le solde peut déjà avoir été en partie dépensé (converti en
            # coupon) avant le remboursement : on reprend au maximum ce qui
            # reste disponible, plutôt que de faire planter tout le flux de
            # remboursement pour un solde de points insuffisant. Le déficit
            # (points déjà dépensés mais gagnés sur un achat depuis
            # remboursé) reste une perte assumée côté fidélité, jamais un
            # blocage du remboursement réel dû au client.
            points_disponibles = compte.solde_points
            points_a_debiter = min(points_a_reprendre, points_disponibles)
            if points_a_debiter <= 0:
                logger.info(
                    f"Reprise de points ignorée pour {client.email} : solde déjà "
                    f"insuffisant ({points_disponibles} pts) pour reprendre "
                    f"{points_a_reprendre} pts suite au retour {demande_retour.numero_retour}."
                )
                return

            try:
                compte.debiter_points(
                    points=points_a_debiter,
                    description=(
                        f"Reprise de points suite au remboursement du retour "
                        f"{demande_retour.numero_retour}"
                    ),
                    reference_externe=demande_retour.numero_retour,
                    type_transaction=TransactionFidelite.TypeTransaction.AJUSTEMENT_ADMIN,
                )
                logger.info(
                    f"{points_a_debiter} points de fidélité repris à {client.email} "
                    f"suite au remboursement {demande_retour.numero_retour}."
                )
            except DjangoValidationError as exc:
                # Course rare : le solde a encore bougé entre notre lecture
                # ci-dessus et le verrou pris par debiter_points(). On log
                # et on abandonne la reprise plutôt que de faire échouer le
                # remboursement réel du client pour cette seule raison.
                logger.warning(
                    f"Reprise de points impossible pour {client.email} "
                    f"(retour {demande_retour.numero_retour}) : {exc}"
                )
        except Exception as e:
            logger.warning(
                f"Erreur lors de la reprise de points de fidélité suite au "
                f"remboursement {getattr(demande_retour, 'numero_retour', '?')}: {e}"
            )

except ImportError:
    logger.warning("Signal 'retour_status_change' indisponible dans apps.fidelite.")
