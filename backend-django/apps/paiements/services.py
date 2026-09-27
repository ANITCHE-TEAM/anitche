"""Paiements : initiation, notifications, validation, remboursements.

Toutes les transitions de Paiement.statut passent par ce module, sous
verrou de ligne (select_for_update) :

    en_attente → valide | echoue | annule
    echoue     → valide      (succès confirmé par le fournisseur après un échec)
    annule     → valide      (succès tardif : paiement annulé par le client ou
                              par l'expiration de la commande)
    valide     → (final)

Un paiement validé pour une commande déjà annulée ou déjà payée par un
autre paiement ne la confirme jamais : un Remboursement est créé.
"""

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.commandes.models import Commande, GroupeCommande

from . import reversements
from .fournisseurs import FournisseurInconnu, fournisseur_actif, obtenir_fournisseur
from .fournisseurs.base import (
    ECHEC,
    EN_ATTENTE,
    SUCCES,
    ErreurFournisseur,
    NotificationInvalide,
    hacher_jeton,
)
from .models import JournalWebhook, Paiement, Remboursement
from .signals import paiement_valide

logger = logging.getLogger(__name__)
logger_securite = logging.getLogger("securite")

STATUTS_ACTIFS = (Paiement.Statut.EN_ATTENTE, Paiement.Statut.VALIDE)
VALIDABLE_DEPUIS = (Paiement.Statut.EN_ATTENTE, Paiement.Statut.ECHOUE, Paiement.Statut.ANNULE)


class PaiementRefuse(Exception):
    """Demande du client refusée (400) — message destiné au client."""


class TransitionPaiementImpossible(Exception):
    """Le paiement n'est pas dans un statut qui permet cette action (409)."""


class IncoherenceTransaction(Exception):
    """L'état confirmé par le fournisseur ne correspond pas au paiement."""


# =====================================================================
# INITIATION
# =====================================================================

MESSAGE_INDISPONIBLE = "Cette commande n'est plus disponible : elle a été annulée et son stock libéré."
MESSAGE_ADRESSE_MANQUANTE = "Adresse de livraison manquante : elle se renseigne à la validation du panier."
MESSAGE_DEJA_EN_COURS = (
    "Un paiement est déjà en cours ou effectué pour cette commande. "
    "Annulez le paiement en attente pour en relancer un autre."
)


def _annuler_si_boutique_indisponible(commandes):
    """Commande non payée d'une boutique qui n'est plus publiable : paiement
    refusé, commande annulée, stock restitué (message volontairement générique)."""
    from apps.commandes.services import TransitionImpossible, annuler_commande

    indisponibles = [c for c in commandes if not c.boutique.est_publiable]
    for commande in indisponibles:
        try:
            annuler_commande(commande, Commande.MotifAnnulation.BOUTIQUE_INDISPONIBLE)
        except TransitionImpossible:
            pass
    if indisponibles:
        raise PaiementRefuse(MESSAGE_INDISPONIBLE)


def _cible(client, commande_id, groupe_commande_id):
    """(commande, groupe, commandes à payer) — uniquement celles du client."""
    if commande_id:
        commande = (
            Commande.objects.select_related("groupe", "boutique__proprietaire")
            .filter(pk=commande_id, client=client).first()
        )
        if commande is None:
            raise PaiementRefuse({"commande_id": "Commande introuvable ou non autorisée."})
        if commande.status == Commande.Status.ANNULEE:
            raise PaiementRefuse("Impossible de payer une commande annulée.")
        if commande.status != Commande.Status.CREEE:
            raise PaiementRefuse("Cette commande a déjà été payée ou confirmée.")
        return commande, commande.groupe, [commande]

    groupe = GroupeCommande.objects.filter(pk=groupe_commande_id, client=client).first()
    if groupe is None:
        raise PaiementRefuse({"groupe_commande_id": "Groupe de commandes introuvable ou non autorisé."})
    commandes = list(
        groupe.commandes.exclude(status=Commande.Status.ANNULEE).select_related("boutique__proprietaire")
    )
    if not commandes:
        raise PaiementRefuse("Ce groupe de commandes ne contient aucune commande à payer.")
    if any(c.status != Commande.Status.CREEE for c in commandes):
        raise PaiementRefuse("Une ou plusieurs commandes de ce groupe ont déjà été validées ou payées.")
    return None, groupe, commandes


def initier_paiement(client, methode, commande_id=None, groupe_commande_id=None):
    """Crée le paiement (montant calculé côté serveur) puis ouvre la
    transaction chez le fournisseur actif.

    Les commandes sont verrouillées pendant la création : deux initiations
    simultanées, ou une commande payée seule puis via son groupe, ne
    peuvent jamais produire deux paiements actifs pour la même commande.
    """
    commande, groupe, commandes = _cible(client, commande_id, groupe_commande_id)
    _annuler_si_boutique_indisponible(commandes)
    if groupe is None or not groupe.a_une_adresse:
        raise PaiementRefuse(MESSAGE_ADRESSE_MANQUANTE)
    fournisseur = fournisseur_actif()

    with transaction.atomic():
        ids = [c.pk for c in commandes]
        verrouillees = list(Commande.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
        if len(verrouillees) != len(ids) or any(c.status != Commande.Status.CREEE for c in verrouillees):
            raise PaiementRefuse("Une ou plusieurs commandes ne sont plus à payer.")
        if Paiement.objects.filter(commandes__in=ids, statut__in=STATUTS_ACTIFS).exists():
            raise PaiementRefuse(MESSAGE_DEJA_EN_COURS)

        montant = sum((c.montant_total for c in verrouillees), 0)
        paiement = Paiement.objects.create(
            client=client,
            commande=commande,
            groupe_commande=groupe if commande is None else None,
            methode=methode,
            fournisseur=fournisseur.code,
            montant=montant,
            adresse_livraison=groupe.adresse_livraison_texte,
        )
        paiement.commandes.set(verrouillees)

    # Appel réseau hors transaction : aucun verrou n'est tenu pendant l'attente.
    try:
        session = fournisseur.initier(paiement)
    except ErreurFournisseur as erreur:
        _passer_statut(paiement, Paiement.Statut.ECHOUE, depuis=(Paiement.Statut.EN_ATTENTE,),
                       motif=f"initiation refusée : {erreur}")
        raise
    paiement.url_paiement = session.url_paiement or None
    paiement.transaction_id_externe = session.identifiant_externe or None
    paiement.hash_jeton_notification = hacher_jeton(session.jeton_notification)
    paiement.save(update_fields=["url_paiement", "transaction_id_externe", "hash_jeton_notification",
                                 "date_mise_a_jour"])
    logger.info("Paiement %s initié (%s, %s, %s FCFA).", paiement.reference, fournisseur.code, methode, montant)
    return paiement


def _passer_statut(paiement, nouveau, depuis, motif=""):
    """Transition simple (échec, annulation) sous verrou. False si le
    paiement n'est plus dans l'un des statuts `depuis`."""
    with transaction.atomic():
        verrouille = Paiement.objects.select_for_update().get(pk=paiement.pk)
        if verrouille.statut not in depuis:
            return False
        verrouille.statut = nouveau
        if motif:
            cle = "motif_echec" if nouveau == Paiement.Statut.ECHOUE else "motif_annulation"
            verrouille.metadata = {**verrouille.metadata, cle: motif}
        verrouille.save(update_fields=["statut", "metadata", "date_mise_a_jour"])
    paiement.refresh_from_db(fields=["statut", "metadata", "date_mise_a_jour"])
    return True


def annuler_paiement_client(paiement):
    """Le client abandonne un paiement en attente pour en relancer un autre.
    Un succès tardif de celui-ci deviendra un remboursement s'il fait doublon."""
    if not _passer_statut(paiement, Paiement.Statut.ANNULE, depuis=(Paiement.Statut.EN_ATTENTE,),
                          motif="annulé par le client"):
        raise TransitionPaiementImpossible("Seul un paiement en attente peut être annulé.")


# =====================================================================
# VALIDATION
# =====================================================================

def valider_paiement(paiement, etat=None):
    """Applique un succès confirmé par le fournisseur. Idempotent : False si
    le paiement était déjà validé (notification rejouée ou concurrente)."""
    from apps.commandes.services import confirmer_commande
    from apps.livraison.models import Livraison

    with transaction.atomic():
        verrouille = Paiement.objects.select_for_update().get(pk=paiement.pk)
        if verrouille.statut not in VALIDABLE_DEPUIS:
            return False
        if etat is not None:
            _controler_etat(verrouille, etat)
            if etat.identifiant_externe:
                verrouille.transaction_id_externe = etat.identifiant_externe
        verrouille.statut = Paiement.Statut.VALIDE
        verrouille.date_validation = timezone.now()
        verrouille.save(update_fields=["statut", "date_validation", "transaction_id_externe", "date_mise_a_jour"])

        confirmees = []
        for commande in verrouille.commandes.select_for_update().order_by("pk"):
            deja_payee = Paiement.objects.filter(
                commandes=commande, statut=Paiement.Statut.VALIDE,
            ).exclude(pk=verrouille.pk).exists()
            if commande.status == Commande.Status.ANNULEE:
                creer_remboursement(verrouille, commande, Remboursement.Motif.COMMANDE_ANNULEE)
            elif deja_payee or not confirmer_commande(commande):
                creer_remboursement(verrouille, commande, Remboursement.Motif.PAIEMENT_EN_DOUBLE)
            else:
                Livraison.objects.get_or_create(
                    commande=commande,
                    defaults={"status": Livraison.Status.EN_ATTENTE, "adresse_livraison": verrouille.adresse_livraison},
                )
                reversements.creer_reversement(commande)
                confirmees.append(commande)

        if confirmees:
            # Fidélité et notifications : uniquement les commandes confirmées.
            paiement_valide.send(
                sender=Paiement,
                paiement=verrouille,
                client=verrouille.client,
                adresse_livraison=verrouille.adresse_livraison,
                commandes=confirmees,
            )
    paiement.refresh_from_db()
    logger.info("Paiement %s validé (%s commande(s) confirmée(s)).", paiement.reference, len(confirmees))
    return True


def _controler_etat(paiement, etat):
    """Montant et devise confirmés par le fournisseur = ceux calculés par le serveur."""
    if etat.montant is not None and etat.montant != int(paiement.montant):
        raise IncoherenceTransaction(
            f"Montant confirmé ({etat.montant}) différent du montant dû ({int(paiement.montant)})."
        )
    if etat.devise is not None and etat.devise != paiement.devise:
        raise IncoherenceTransaction(f"Devise confirmée ({etat.devise}) différente de {paiement.devise}.")


def marquer_echoue(paiement, motif="échec confirmé par le fournisseur"):
    return _passer_statut(paiement, Paiement.Statut.ECHOUE, depuis=(Paiement.Statut.EN_ATTENTE,), motif=motif)


# =====================================================================
# NOTIFICATIONS DES FOURNISSEURS
# =====================================================================

class ResultatNotification:
    def __init__(self, code_http, message):
        self.code_http, self.message = code_http, message


def _journaliser(journal, statut, erreur=""):
    journal.statut_traitement = statut
    journal.erreur = erreur
    journal.save(update_fields=["statut_traitement", "erreur"])


def _ouvrir_journal(code, notification):
    """(journal, deja_traite). Le même événement reçu deux fois n'est traité
    qu'une fois ; un événement en erreur ou non final peut être rejoué."""
    try:
        journal, cree = JournalWebhook.objects.get_or_create(
            fournisseur=code, evenement_id=notification.cle_idempotence[:150],
            defaults={"payload": notification.donnees},
        )
    except IntegrityError:
        journal, cree = JournalWebhook.objects.get(fournisseur=code, evenement_id=notification.cle_idempotence[:150]), False
    return journal, (not cree and journal.statut_traitement == JournalWebhook.StatutTraitement.TRAITE)


def traiter_notification_paiement(code, request):
    """Notification d'un fournisseur → authentification → vérification
    serveur à serveur → application sous verrou. Renvoie ResultatNotification."""
    try:
        fournisseur = obtenir_fournisseur(code)
        notification = fournisseur.lire_notification(request)
    except FournisseurInconnu:
        return ResultatNotification(404, "Fournisseur inconnu.")
    except NotificationInvalide as erreur:
        logger_securite.warning("Notification %s refusée : %s (ip=%s)", code, erreur, request.META.get("REMOTE_ADDR"))
        return ResultatNotification(401, "Notification non authentique.")

    # Le paiement doit avoir été ouvert chez CE fournisseur.
    paiement = Paiement.objects.filter(reference=notification.reference, fournisseur=code).first()
    if paiement is None:
        logger_securite.warning("Notification %s pour un paiement inconnu : %s", code, notification.reference)
        return ResultatNotification(404, "Paiement inconnu.")
    try:
        fournisseur.authentifier_notification(notification, paiement)
    except NotificationInvalide as erreur:
        logger_securite.warning("Notification %s refusée pour %s : %s", code, paiement.reference, erreur)
        return ResultatNotification(401, "Notification non authentique.")

    journal, deja_traite = _ouvrir_journal(code, notification)
    if deja_traite:
        return ResultatNotification(200, "Événement déjà traité.")

    try:
        etat = fournisseur.verifier_transaction(paiement, notification)
    except ErreurFournisseur as erreur:
        _journaliser(journal, JournalWebhook.StatutTraitement.ERREUR, str(erreur))
        return ResultatNotification(503, "Vérification auprès du fournisseur impossible, réessayez.")
    except NotificationInvalide as erreur:
        _journaliser(journal, JournalWebhook.StatutTraitement.ERREUR, str(erreur))
        logger_securite.warning("Notification %s incohérente pour %s : %s", code, paiement.reference, erreur)
        return ResultatNotification(400, "Notification incohérente.")

    try:
        if etat.statut == SUCCES:
            valider_paiement(paiement, etat)
        elif etat.statut == ECHEC:
            marquer_echoue(paiement)
        else:
            _journaliser(journal, JournalWebhook.StatutTraitement.IGNORE)
            return ResultatNotification(200, "Transaction non finalisée.")
    except IncoherenceTransaction as erreur:
        _journaliser(journal, JournalWebhook.StatutTraitement.ERREUR, str(erreur))
        logger_securite.error("Paiement %s rejeté : %s", paiement.reference, erreur)
        return ResultatNotification(400, "Transaction incohérente avec le paiement.")

    _journaliser(journal, JournalWebhook.StatutTraitement.TRAITE)
    return ResultatNotification(200, f"Paiement {paiement.reference} traité.")


# =====================================================================
# REMBOURSEMENTS
# =====================================================================

def creer_remboursement(paiement, commande, motif, montant=None, retour=None):
    """Remboursement à traiter (une seule fois par paiement et commande, ou
    par retour) + alerte de l'administration. Renvoie (remboursement, cree)."""
    montant = commande.montant_total if montant is None else montant
    with transaction.atomic():
        if retour is not None:
            remboursement, cree = Remboursement.objects.get_or_create(
                retour=retour,
                defaults={"paiement": paiement, "commande": commande, "motif": motif, "montant": montant},
            )
        else:
            remboursement, cree = Remboursement.objects.get_or_create(
                paiement=paiement, commande=commande, retour=None,
                defaults={"motif": motif, "montant": montant},
            )
    if cree:
        alerter_administration(remboursement)
    return remboursement, cree


def alerter_administration(remboursement):
    """Journal de sécurité + notification de chaque administrateur actif."""
    from apps.notifications.models import Notification
    from apps.notifications.services import ServiceNotification

    commande = remboursement.commande
    message = (
        f"Remboursement {remboursement.reference} à traiter : commande {commande.numero_commande} "
        f"({int(remboursement.montant)} FCFA) — {remboursement.get_motif_display()}."
    )
    logger_securite.error("ALERTE ADMINISTRATION — %s", message)
    ServiceNotification.notifier_administration(
        titre="Remboursement à traiter",
        message=message,
        type_notification=Notification.TypeNotification.PAIEMENT,
        metadata={"remboursement": str(remboursement.pk), "commande": str(commande.pk)},
    )


def traiter_remboursement(remboursement, administrateur, decision, reference_externe="", commentaire=""):
    """L'administration a remboursé depuis le tableau de bord du fournisseur
    (effectue, référence obligatoire) ou refuse le remboursement (refuse)."""
    from apps.notifications.models import Notification
    from apps.notifications.services import ServiceNotification

    with transaction.atomic():
        verrouille = Remboursement.objects.select_for_update().select_related("paiement__client").get(pk=remboursement.pk)
        if verrouille.statut != Remboursement.Statut.A_TRAITER:
            raise TransitionPaiementImpossible("Ce remboursement a déjà été traité.")
        verrouille.statut = decision
        verrouille.reference_externe = reference_externe
        verrouille.commentaire = commentaire
        verrouille.traite_par = administrateur
        verrouille.date_traitement = timezone.now()
        verrouille.save()
    logger_securite.info(
        "Remboursement %s %s par admin_id=%s (réf. %s)",
        verrouille.reference, decision, administrateur.id, reference_externe or "-",
    )
    if decision == Remboursement.Statut.EFFECTUE:
        ServiceNotification.notifier_utilisateur(
            verrouille.paiement.client,
            titre="Remboursement effectué",
            message=f"Votre remboursement de {int(verrouille.montant)} FCFA ({verrouille.reference}) a été effectué.",
            type_notification=Notification.TypeNotification.PAIEMENT,
            metadata={"remboursement": str(verrouille.pk)},
        )
    return verrouille


# =====================================================================
# LIENS AVEC LES COMMANDES ET LES RETOURS
# =====================================================================

def paiement_valide_de(commande):
    """Paiement encaissé qui a confirmé la commande (le premier validé)."""
    return commande.paiements_couvrants.filter(statut=Paiement.Statut.VALIDE).order_by("date_validation").first()


def traiter_paiements_apres_annulation(commande):
    """Appelée à l'annulation d'une commande (dans sa transaction) :
    paiement encaissé → remboursement à traiter et reversement annulé ;
    paiement en attente dont toutes les commandes sont annulées → annulé."""
    for paiement in commande.paiements_couvrants.filter(statut__in=STATUTS_ACTIFS):
        if paiement.statut == Paiement.Statut.VALIDE:
            creer_remboursement(paiement, commande, Remboursement.Motif.COMMANDE_ANNULEE)
        elif not paiement.commandes.exclude(status=Commande.Status.ANNULEE).exists():
            _passer_statut(paiement, Paiement.Statut.ANNULE, depuis=(Paiement.Statut.EN_ATTENTE,),
                           motif="commande annulée")
    reversements.annuler_reversement(commande)


def rembourser_retour(demande_retour):
    """Retour remboursé : remboursement à traiter pour le client (articles,
    et frais de livraison si le motif est imputable au vendeur), part du
    vendeur réduite (avant versement) ou ajustement négatif (après), et
    frais de livraison remboursés facturés au vendeur (ajustement)."""
    commande = demande_retour.commande
    paiement = paiement_valide_de(commande)
    if paiement is None:
        logger_securite.error("Retour %s remboursé sans paiement encaissé.", demande_retour.numero_retour)
        return None
    remboursement, cree = creer_remboursement(
        paiement, commande, Remboursement.Motif.RETOUR,
        montant=demande_retour.montant_remboursement, retour=demande_retour,
    )
    # Une seule déduction par retour : le Remboursement (unique par retour)
    # sert de marqueur. Un second appel ne réduit pas deux fois la part du
    # vendeur (montant_retours n'est pas idempotent à lui seul).
    if cree:
        reversements.appliquer_retour_rembourse(demande_retour)
        reversements.facturer_frais_livraison_retour(demande_retour)
    return remboursement
