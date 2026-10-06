"""Distribution des notifications (docs/MODULE_NOTIFICATIONS.md).

La notification in-app est créée dans la transaction de l'action : annulée
avec elle en cas d'erreur. L'email part APRÈS le commit, par Celery : jamais
d'appel SMTP sous un verrou de base, jamais d'email pour une action annulée.
"""

import logging

from django.db import transaction
from django.utils import timezone

from . import liens
from .models import Notification, PreferenceNotification

logger = logging.getLogger(__name__)

Type = Notification.TypeNotification


def email_souhaite(utilisateur):
    """Préférence email (vraie par défaut, sans créer de ligne)."""
    valeur = PreferenceNotification.objects.filter(utilisateur=utilisateur).values_list("email_actif", flat=True).first()
    return True if valeur is None else valeur


def _programmer_email(notification):
    from .tasks import envoyer_email_notification

    transaction.on_commit(lambda: envoyer_email_notification.delay(str(notification.pk)))


class ServiceNotification:
    """Service centralisé de distribution des notifications."""

    @staticmethod
    def get_preferences(utilisateur):
        """Récupère ou initialise les préférences d'un utilisateur."""
        prefs, _ = PreferenceNotification.objects.get_or_create(utilisateur=utilisateur)
        return prefs

    @classmethod
    def notifier_utilisateur(cls, destinataire, titre, message, type_notification=Type.SYSTEME, lien_redirection="",
                             metadata=None, email=True, email_critique=False):
        """Notification in-app (toujours) + email après le commit si
        `email` et que le destinataire ne l'a pas désactivé. `email_critique` :
        envoyé malgré la préférence (information indispensable)."""
        notification = Notification.objects.create(
            destinataire=destinataire,
            titre=titre[:200],
            message=message,
            type_notification=type_notification,
            canal=Notification.Canal.IN_APP,
            lien_redirection=lien_redirection[:255],
            metadata=metadata or {},
        )
        if email and destinataire.email and (email_critique or email_souhaite(destinataire)):
            _programmer_email(notification)
        # Identifiants seulement : ni email, ni téléphone, ni contenu dans les journaux.
        logger.info("Notification %s créée (destinataire_id=%s, type=%s).", notification.pk, destinataire.pk,
                    type_notification)
        return [notification]

    @staticmethod
    def notifier_administration(titre, message, type_notification=Type.SYSTEME, lien_redirection="", metadata=None):
        """Alerte de chaque administrateur actif, in-app seulement (un email
        par alerte et par administrateur noyait les boîtes ; le journal
        `securite` garde la trace)."""
        from apps.utilisateurs.models import Utilisateur
        from apps.vendeurs.permissions import ROLES_ADMINISTRATION

        administrateurs = Utilisateur.objects.filter(role__in=ROLES_ADMINISTRATION, is_active=True).only("pk")
        return Notification.objects.bulk_create([
            Notification(destinataire=administrateur, titre=titre[:200], message=message,
                         type_notification=type_notification, lien_redirection=lien_redirection[:255],
                         metadata=metadata or {})
            for administrateur in administrateurs
        ])

    @staticmethod
    def marquer_toutes_lues(destinataire):
        """Marque toutes les notifications non lues d'un utilisateur comme lues."""
        return Notification.objects.filter(destinataire=destinataire, est_lu=False).update(
            est_lu=True, date_lecture=timezone.now(),
        )


# =====================================================================
# ALERTES MÉTIER
# =====================================================================

def alerter_stock_bas(stock, avant, apres):
    """Le vendeur est prévenu quand une vente fait passer une variante sous
    son seuil d'alerte (une seule fois : au franchissement), ou en rupture."""
    seuil = stock.seuil_alerte
    if not (avant > seuil >= apres or (avant > 0 and apres == 0)):
        return None
    variante = stock.variante
    produit = variante.produit
    rupture = apres == 0
    return ServiceNotification.notifier_utilisateur(
        produit.boutique.proprietaire,
        titre=f"{'Rupture de stock' if rupture else 'Stock bas'} : {produit.nom}"[:200],
        message=(
            f"{produit.nom} ({variante.nom}) : "
            + ("plus aucune unité disponible, le produit n'est plus commandable."
               if rupture else f"{apres} unité(s) restante(s) (seuil d'alerte : {seuil}).")
        ),
        type_notification=Type.STOCK,
        lien_redirection=liens.lien_produit_vendeur(produit),
        metadata={"variante_id": str(variante.pk), "quantite_disponible": apres, "seuil_alerte": seuil},
    )


def notifier_annulation_commande(commande, etait_payee):
    """Client : toujours (motif, et remboursement si payée). Vendeur :
    seulement si la commande était payée (une commande impayée expirée ne
    le concerne pas)."""
    motif = commande.get_motif_annulation_display() if commande.motif_annulation else "annulée"
    ServiceNotification.notifier_utilisateur(
        commande.client,
        titre=f"Commande {commande.numero_commande} annulée",
        message=(
            f"Votre commande {commande.numero_commande} a été annulée ({motif})."
            + (" Le remboursement de votre paiement est en cours de traitement." if etait_payee else "")
        ),
        type_notification=Type.COMMANDE,
        lien_redirection=liens.lien_commande_client(commande),
        metadata={"commande_id": str(commande.pk), "motif": commande.motif_annulation},
    )
    if etait_payee:
        ServiceNotification.notifier_utilisateur(
            commande.boutique.proprietaire,
            titre=f"Commande {commande.numero_commande} annulée",
            message=f"La commande {commande.numero_commande} a été annulée ({motif}) : ne la préparez pas ou plus.",
            type_notification=Type.COMMANDE,
            lien_redirection=liens.lien_commande_vendeur(commande),
            metadata={"commande_id": str(commande.pk), "motif": commande.motif_annulation},
        )
