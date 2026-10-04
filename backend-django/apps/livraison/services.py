"""Cycle de vie d'une livraison : seul point d'entrée pour la modifier.

Table des transitions (docs/MODULE_LIVRAISON.md) :

    en_attente → expediee    livreur assigné (commande en préparation)
    expediee   → en_cours    livreur assigné (code de livraison généré, envoyé au client)
    expediee   → echouee     livreur assigné, motif obligatoire
    en_cours   → livree      livreur assigné + code donné par le client
    en_cours   → echouee     livreur assigné, motif obligatoire (ex : client injoignable)
    echouee    → en_cours    administration : nouvelle tentative (LIVRAISON_TENTATIVES_MAX)
    en_attente → annulee     système : commande annulée (commandes.services.annuler_commande)
    echouee    → annulee     administration : échec définitif (commande annulée, remboursement)

Tout le reste est refusé, administration comprise (plus de saut libre) :
« livrée » déclenche le reversement au vendeur. Chaque changement se fait
sous verrou de la fiche (select_for_update) : deux requêtes simultanées
ne peuvent pas partir du même statut.
"""

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction
from django.utils import timezone

from apps.utilisateurs.models import Role
from apps.vendeurs.permissions import ROLES_ADMINISTRATION

from .models import ContestationLivraison, Livraison, LivraisonHistorique
from .signals import livraison_status_change

logger = logging.getLogger(__name__)
logger_securite = logging.getLogger("securite")

Statut = Livraison.Status

LIVREUR = "livreur"
ADMINISTRATION = "administration"
SYSTEME = "systeme"

#: (depuis, vers) → qui peut faire la transition par l'API.
TRANSITIONS = {
    (Statut.EN_ATTENTE, Statut.EXPEDIEE): LIVREUR,
    (Statut.EXPEDIEE, Statut.EN_COURS): LIVREUR,
    (Statut.EXPEDIEE, Statut.ECHOUEE): LIVREUR,
    (Statut.EN_COURS, Statut.LIVREE): LIVREUR,
    (Statut.EN_COURS, Statut.ECHOUEE): LIVREUR,
    (Statut.ECHOUEE, Statut.EN_COURS): ADMINISTRATION,
}

#: Statuts où la livraison est terminée : plus d'assignation, et le
#: livreur ne voit plus les coordonnées du client.
STATUTS_TERMINES = (Statut.LIVREE, Statut.ANNULEE)

CODE_ESSAIS_MAX = 5


class LivraisonRefusee(Exception):
    """Action refusée : message pour l'appelant, code HTTP à renvoyer."""

    def __init__(self, message, code_http=400):
        super().__init__(message)
        self.code_http = code_http


# =====================================================================
# OUTILS
# =====================================================================

def role_acteur(utilisateur):
    if utilisateur is None:
        return SYSTEME
    if utilisateur.role in ROLES_ADMINISTRATION:
        return ADMINISTRATION
    if utilisateur.role == Role.LIVREUR:
        return LIVREUR
    return utilisateur.role


def _verrouiller(livraison):
    return Livraison.objects.select_for_update().select_related("commande__boutique", "commande__client").get(
        pk=livraison.pk
    )


def _historiser(livraison, ancien, nouveau, acteur, commentaire=""):
    LivraisonHistorique.objects.create(
        livraison=livraison, ancien_status=ancien, nouveau_status=nouveau,
        effectue_par=acteur, role_acteur=role_acteur(acteur), commentaire=commentaire[:255],
    )


def _appliquer(livraison, nouveau, acteur, commentaire="", champs=()):
    """Écrit le nouveau statut (fiche déjà verrouillée), historise, émet le signal."""
    ancien = livraison.status
    livraison.status = nouveau
    livraison.save(update_fields=["status", "updated_at", *champs])
    _historiser(livraison, ancien, nouveau, acteur, commentaire)
    livraison_status_change.send(
        sender=Livraison, livraison=livraison, ancien_status=ancien,
        nouveau_status=nouveau, effectue_par=acteur,
    )


def livreur_eligible(utilisateur, commande):
    """Motif du refus, ou None si l'utilisateur peut livrer cette commande.

    Jamais le propriétaire de la boutique : il déclencherait lui-même le
    reversement de sa propre vente."""
    if utilisateur.role != Role.LIVREUR:
        return "Cet utilisateur n'est pas livreur."
    if not utilisateur.is_active:
        return "Ce livreur est désactivé."
    if commande.boutique.proprietaire_id == utilisateur.pk:
        return "Le propriétaire de la boutique ne peut pas livrer sa propre commande."
    return None


def _nouveau_code(livraison):
    code = f"{secrets.randbelow(1000000):06d}"
    livraison.code_hash = make_password(code)
    livraison.code_chiffre = code
    livraison.code_essais = 0


def _effacer_code(livraison):
    livraison.code_hash = ""
    livraison.code_chiffre = ""


def _envoyer_code_apres_validation(livraison):
    from .tasks import envoyer_code_livraison_email

    transaction.on_commit(lambda: envoyer_code_livraison_email.delay(str(livraison.pk)))


# =====================================================================
# ASSIGNATION
# =====================================================================

def assigner_livreur(livraison, livreur, administrateur, date_livraison_estimee=None):
    """Assigne (ou réassigne) un livreur, tant que la livraison n'est pas terminée."""
    with transaction.atomic():
        livraison = _verrouiller(livraison)
        if livraison.status in STATUTS_TERMINES:
            raise LivraisonRefusee("Livraison terminée : elle ne peut plus être assignée.")
        motif = livreur_eligible(livreur, livraison.commande)
        if motif:
            raise LivraisonRefusee(motif)
        ancien_livreur_id = livraison.livreur_id
        livraison.livreur = livreur
        champs = ["livreur", "updated_at"]
        if date_livraison_estimee is not None:
            livraison.date_livraison_estimee = date_livraison_estimee
            champs.append("date_livraison_estimee")
        livraison.save(update_fields=champs)
        _historiser(
            livraison, livraison.status, livraison.status, administrateur,
            f"{'Réassignée' if ancien_livreur_id not in (None, livreur.pk) else 'Assignée'} "
            f"à {livreur.prenom} {livreur.nom[:1]}.",
        )
    logger_securite.info(
        "Livraison %s assignée au livreur_id=%s (avant : %s) par admin_id=%s",
        livraison.pk, livreur.pk, ancien_livreur_id, getattr(administrateur, "pk", None),
    )
    return livraison


# =====================================================================
# CHANGEMENT DE STATUT (API)
# =====================================================================

def _controler_acteur(livraison, acteur, qui):
    if qui == ADMINISTRATION:
        if acteur.role not in ROLES_ADMINISTRATION:
            raise LivraisonRefusee("Action réservée à l'administration ANITCHE.", 403)
        return
    if livraison.livreur_id != acteur.pk or livreur_eligible(acteur, livraison.commande):
        raise LivraisonRefusee("Vous n'êtes pas autorisé à modifier cette livraison.", 403)


def changer_statut(livraison, nouveau, acteur, commentaire="", code=""):
    """Transition demandée par un livreur ou l'administration.

    Lève LivraisonRefusee (400/403) ou commandes.services.TransitionImpossible
    (commande pas prête : rien n'est changé)."""
    from apps.commandes.services import synchroniser_depuis_livraison

    code_refuse = None
    with transaction.atomic():
        livraison = _verrouiller(livraison)
        ancien = livraison.status
        qui = TRANSITIONS.get((ancien, nouveau))
        if qui is None:
            # Contrôle d'accès d'abord : ne rien révéler de l'état à un tiers.
            if not (acteur.role in ROLES_ADMINISTRATION or livraison.livreur_id == acteur.pk):
                raise LivraisonRefusee("Vous n'êtes pas autorisé à modifier cette livraison.", 403)
            raise LivraisonRefusee(
                f"Transition invalide : impossible de passer de '{ancien}' à '{nouveau}'."
            )
        _controler_acteur(livraison, acteur, qui)

        champs = []
        if nouveau == Statut.ECHOUEE and not commentaire.strip():
            raise LivraisonRefusee("Le motif de l'échec est obligatoire (commentaire).")

        if nouveau == Statut.EN_COURS:
            if livraison.tentatives >= settings.LIVRAISON_TENTATIVES_MAX:
                raise LivraisonRefusee(
                    f"Nombre maximal de tentatives atteint ({settings.LIVRAISON_TENTATIVES_MAX}) : "
                    "l'administration doit abandonner la livraison."
                )
            livraison.tentatives += 1
            _nouveau_code(livraison)
            champs += ["tentatives", "code_hash", "code_chiffre", "code_essais"]

        if nouveau == Statut.LIVREE:
            if livraison.code_essais >= CODE_ESSAIS_MAX:
                raise LivraisonRefusee(
                    "Code de livraison bloqué après trop d'essais : marquez la livraison échouée."
                )
            if not (code and livraison.code_hash and check_password(str(code), livraison.code_hash)):
                livraison.code_essais += 1
                livraison.save(update_fields=["code_essais", "updated_at"])
                restants = CODE_ESSAIS_MAX - livraison.code_essais
                if not restants:
                    logger_securite.warning(
                        "Code de livraison bloqué (livraison_id=%s, livreur_id=%s)", livraison.pk, acteur.pk,
                    )
                code_refuse = f"Code de livraison incorrect ({restants} essai(s) restant(s))."
            else:
                livraison.date_livraison = timezone.now()
                _effacer_code(livraison)
                champs += ["date_livraison", "code_hash", "code_chiffre"]

        if code_refuse is None:
            if nouveau == Statut.EXPEDIEE and not livraison.date_expedition:
                livraison.date_expedition = timezone.now()
                champs.append("date_expedition")
            # Expédiée / livrée se répercutent sur la commande (livrée ouvre
            # le délai de rétractation du reversement).
            synchroniser_depuis_livraison(livraison.commande, nouveau)
            _appliquer(livraison, nouveau, acteur, commentaire, champs)

    if code_refuse:
        raise LivraisonRefusee(code_refuse)
    if nouveau == Statut.EN_COURS:
        _envoyer_code_apres_validation(livraison)
    return livraison


# =====================================================================
# ANNULATION ET ÉCHEC DÉFINITIF
# =====================================================================

def annuler_livraison_de(commande, acteur=None, commentaire="Commande annulée."):
    """Appelée par commandes.services.annuler_commande, dans sa transaction.
    Renvoie la fiche lue, ou None si la commande n'en a pas.

    Refuse (TransitionImpossible, donc aucune annulation) une livraison déjà
    partie : seule une livraison en attente ou échouée s'annule."""
    from apps.commandes.services import TransitionImpossible

    livraison = Livraison.objects.select_for_update().filter(commande=commande).first()
    if livraison is None or livraison.status == Statut.ANNULEE:
        return livraison
    if livraison.status not in (Statut.EN_ATTENTE, Statut.ECHOUEE):
        raise TransitionImpossible(
            f"La livraison de la commande {commande.numero_commande} est "
            f"« {livraison.get_status_display()} » : elle ne peut pas être annulée."
        )
    _effacer_code(livraison)
    _appliquer(livraison, Statut.ANNULEE, acteur, commentaire, ["code_hash", "code_chiffre"])
    return livraison


def abandonner_livraison(livraison, administrateur, commentaire):
    """Échec définitif : la commande « expédiée » est annulée (motif
    livraison échouée), le paiement devient un remboursement à traiter."""
    from apps.commandes.models import Commande
    from apps.commandes.services import annuler_commande

    if not commentaire.strip():
        raise LivraisonRefusee("Le motif de l'abandon est obligatoire (commentaire).")
    with transaction.atomic():
        livraison = _verrouiller(livraison)
        if livraison.status != Statut.ECHOUEE:
            raise LivraisonRefusee("Seule une livraison échouée peut être abandonnée.")
        annuler_commande(
            livraison.commande, Commande.MotifAnnulation.LIVRAISON_ECHOUEE,
            acteur=administrateur, commentaire=commentaire,
        )
    logger_securite.info(
        "Livraison %s abandonnée par admin_id=%s : %s", livraison.pk, administrateur.pk, commentaire,
    )
    livraison.refresh_from_db()
    return livraison


# =====================================================================
# CONTESTATION « NON REÇU »
# =====================================================================

def fin_du_delai_de_contestation(livraison):
    return livraison.date_livraison + timedelta(days=settings.REVERSEMENT_DELAI_RETRACTATION_JOURS)


def contestation_ouverte(commande):
    """Utilisée par paiements.reversements : bloque la reprise du reversement."""
    return ContestationLivraison.objects.filter(
        livraison__commande=commande, statut=ContestationLivraison.Statut.OUVERTE,
    ).exists()


def _alerter_administration(titre, message, metadata):
    from apps.notifications.models import Notification
    from apps.notifications.services import ServiceNotification

    logger_securite.warning("ALERTE ADMINISTRATION — %s", message)
    ServiceNotification.notifier_administration(
        titre=titre, message=message, type_notification=Notification.TypeNotification.LIVRAISON, metadata=metadata,
    )


def contester_livraison(livraison, client, motif):
    """Le client signale « non reçu » pendant le délai de rétractation :
    le reversement au vendeur est suspendu."""
    from apps.paiements.reversements import suspendre_reversement

    if not motif.strip():
        raise LivraisonRefusee("Décrivez le problème (motif).")
    with transaction.atomic():
        livraison = _verrouiller(livraison)
        if livraison.commande.client_id != client.pk:
            raise LivraisonRefusee("Livraison introuvable.", 404)
        if livraison.status != Statut.LIVREE:
            raise LivraisonRefusee("Seule une livraison marquée livrée peut être contestée.")
        if timezone.now() > fin_du_delai_de_contestation(livraison):
            raise LivraisonRefusee("Le délai de contestation est écoulé.")
        if ContestationLivraison.objects.filter(livraison=livraison).exists():
            raise LivraisonRefusee("Cette livraison a déjà été contestée.")
        contestation = ContestationLivraison.objects.create(livraison=livraison, motif=motif.strip()[:1000])
        suspendre_reversement(livraison.commande)
    _alerter_administration(
        "Livraison contestée",
        f"Le client signale la commande {livraison.commande.numero_commande} comme non reçue "
        f"(livreur_id={livraison.livreur_id}) : reversement suspendu.",
        {"livraison_id": str(livraison.pk), "contestation_id": str(contestation.pk)},
    )
    return contestation


def resoudre_contestation(livraison, administrateur, decision, commentaire=""):
    """Rejetée : le reversement reprend. Fondée : le reversement est annulé
    et le paiement devient un remboursement à traiter."""
    from apps.notifications.models import Notification
    from apps.notifications.services import ServiceNotification
    from apps.paiements import reversements
    from apps.paiements.models import Remboursement
    from apps.fidelite.services import annuler_gain
    from apps.paiements.services import creer_remboursement, paiement_valide_de

    Decision = ContestationLivraison.Statut
    if decision not in (Decision.REJETEE, Decision.FONDEE):
        raise LivraisonRefusee("Décision attendue : « rejetee » ou « fondee ».")
    with transaction.atomic():
        contestation = ContestationLivraison.objects.select_for_update().select_related(
            "livraison__commande__client",
        ).filter(livraison=livraison).first()
        if contestation is None:
            raise LivraisonRefusee("Aucune contestation pour cette livraison.", 404)
        if contestation.statut != Decision.OUVERTE:
            raise LivraisonRefusee("Cette contestation a déjà été traitée.")
        contestation.statut = decision
        contestation.commentaire_resolution = commentaire[:255]
        contestation.resolue_par = administrateur
        contestation.date_resolution = timezone.now()
        contestation.save()
        commande = contestation.livraison.commande
        if decision == Decision.REJETEE:
            reversements.reprendre_reversement(commande)
        else:
            paiement = paiement_valide_de(commande)
            if paiement is not None:
                creer_remboursement(paiement, commande, Remboursement.Motif.LIVRAISON_NON_RECUE)
            reversements.annuler_reversement(commande)
            # Colis jamais reçu : les points en attente de la commande sont annulés.
            annuler_gain(commande, "Livraison contestée : colis non reçu")
    logger_securite.info(
        "Contestation livraison %s %s par admin_id=%s", livraison.pk, decision, administrateur.pk,
    )
    ServiceNotification.notifier_utilisateur(
        commande.client,
        titre=f"Contestation de la commande {commande.numero_commande}",
        message=(
            "Votre contestation est fondée : un remboursement va être effectué."
            if decision == Decision.FONDEE
            else "Après vérification, votre contestation n'a pas été retenue."
        ),
        type_notification=Notification.TypeNotification.LIVRAISON,
        metadata={"livraison_id": str(livraison.pk)},
    )
    return contestation


# =====================================================================
# RÔLE LIVREUR
# =====================================================================

def nommer_livreur(utilisateur, administrateur):
    """Client → livreur. Refus pour un vendeur (ou une demande vendeur en
    cours) et pour tout autre rôle : un compte ne porte qu'un rôle."""
    from apps.utilisateurs.models import StatutKYC, Utilisateur
    from apps.vendeurs.models import Boutique

    with transaction.atomic():
        utilisateur = Utilisateur.objects.select_for_update().get(pk=utilisateur.pk)
        if utilisateur.role == Role.LIVREUR:
            raise LivraisonRefusee("Cet utilisateur est déjà livreur.")
        if utilisateur.role == Role.VENDEUR or utilisateur.statut_kyc in (StatutKYC.EN_ATTENTE, StatutKYC.VALIDE) \
                or Boutique.objects.filter(proprietaire=utilisateur).exists():
            raise LivraisonRefusee("Un vendeur (ou une demande vendeur en cours) ne peut pas être livreur.")
        if utilisateur.role != Role.CLIENT:
            raise LivraisonRefusee("Seul un compte client peut être nommé livreur.")
        if not utilisateur.is_active:
            raise LivraisonRefusee("Ce compte est désactivé.")
        utilisateur.role = Role.LIVREUR
        utilisateur.save(update_fields=["role", "date_mise_a_jour"])
    logger_securite.info("Utilisateur %s nommé livreur par admin_id=%s", utilisateur.pk, administrateur.pk)
    return utilisateur


def livraisons_non_terminees(livreur):
    return Livraison.objects.filter(livreur=livreur).exclude(status__in=STATUTS_TERMINES).select_related("commande")


def retirer_livreur(utilisateur, administrateur):
    """Livreur → client, effet immédiat (plus aucune action possible sur
    ses livraisons). Renvoie ses livraisons non terminées, à réassigner."""
    from apps.utilisateurs.models import Utilisateur

    with transaction.atomic():
        utilisateur = Utilisateur.objects.select_for_update().get(pk=utilisateur.pk)
        if utilisateur.role != Role.LIVREUR:
            raise LivraisonRefusee("Cet utilisateur n'est pas livreur.")
        utilisateur.role = Role.CLIENT
        utilisateur.save(update_fields=["role", "date_mise_a_jour"])
    a_reassigner = list(livraisons_non_terminees(utilisateur))
    logger_securite.info(
        "Livreur %s retiré par admin_id=%s (%s livraison(s) à réassigner)",
        utilisateur.pk, administrateur.pk, len(a_reassigner),
    )
    return utilisateur, a_reassigner
