"""Reversements aux vendeurs : ANITCHE encaisse, puis reverse après
livraison confirmée et délai de rétractation, frais déduits.

    en_attente_livraison → en_retractation   commande livrée (date_disponibilite = +N jours)
    en_retractation      → disponible        délai écoulé (tâche périodique)
    en_retractation / disponible → suspendu  retour ouvert
    suspendu             → en_retractation / disponible   retour clos (et aucune contestation de livraison ouverte)
    disponible           → en_cours → verse  transfert par le fournisseur
    disponible           → verse             versement manuel (référence saisie)
    en_cours             → disponible        refus certain ou échec confirmé par le fournisseur
    en_cours             → disponible / verse   résolution manuelle (commande resoudre_reversement)
    tout sauf verse      → annule            commande annulée (sauf en_cours)

Un transfert dont l'issue est incertaine (délai dépassé, panne ou réponse
illisible après l'envoi) reste en_cours : l'argent a pu partir, le
reversement ne redevient jamais disponible sans confirmation du fournisseur,
et ni nouveau transfert ni versement manuel ne sont possibles entre-temps.
Tout état renvoyé par le fournisseur passe par appliquer_etat_transfert.
La réconciliation (tâche planifiée, commande reconcilier_reversements)
redemande l'état des transferts en cours dont la notification n'arrive pas,
et alerte l'administration pour ceux qui restent bloqués.

Frais de livraison : jamais dans le reversement (ils reviennent à ANITCHE).
Livraison offerte par la boutique : son tarif est déduit du net, plafonné
au net ; le reste devient un ajustement négatif une fois la commande livrée.

Canal : mobile money uniquement, sur le numéro du dossier KYC du vendeur.
"""

import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from .fournisseurs import FournisseurInconnu, fournisseur_actif, obtenir_fournisseur
from .fournisseurs.base import (
    ECHEC,
    SUCCES,
    ErreurFournisseur,
    MontantHorsLimites,
    NotificationInvalide,
    OperationNonSupportee,
    TransactionIntrouvable,
    TransfertRefuse,
    hacher_jeton,
)
from .models import AjustementVendeur, JournalWebhook, Reversement

logger = logging.getLogger(__name__)
logger_securite = logging.getLogger("securite")

Statut = Reversement.Statut
OPERATEURS = ("wave", "orange_money", "mtn_money", "moov_money")
#: Préfixes ivoiriens (numérotation à 10 chiffres) → opérateur. Wave
#: fonctionne sur tout numéro : il se choisit explicitement.
PREFIXES_OPERATEURS = {"07": "orange_money", "05": "mtn_money", "01": "moov_money"}
#: Devise des transferts (mobile money ivoirien) : celle que le fournisseur
#: doit confirmer pour qu'un transfert réussi soit appliqué.
DEVISE_TRANSFERT = "XOF"
#: Erreurs de transferer qui garantissent qu'aucun transfert n'a été créé :
#: seules à rendre le reversement disponible. Toute autre ErreurFournisseur
#: laisse l'issue incertaine (fermé par défaut).
REFUS_CERTAINS = (TransfertRefuse, MontantHorsLimites, OperationNonSupportee)
#: Issue d'un état de transfert refusé (succès non conforme), en plus de
#: SUCCES, ECHEC et EN_ATTENTE.
REFUSE = "refuse"
#: Issues de reconcilier_reversement en plus des précédentes.
INJOIGNABLE = "injoignable"
INTROUVABLE = "introuvable"
NON_DISPONIBLE = "non_disponible"
#: Une alerte sur un reversement en cours (bloqué, succès non conforme) se
#: répète au plus une fois par période tant qu'il le reste.
REPETITION_ALERTE = timedelta(hours=24)
#: Libellé de la source d'un état de transfert dans les journaux.
SOURCES = {"immediat": "immédiat", "notification": "notifié", "reconciliation": "constaté par réconciliation"}


class ErreurVersement(Exception):
    """Versement impossible (statut, numéro, montant) — message pour l'admin."""


# =====================================================================
# CYCLE DE VIE
# =====================================================================

def creer_reversement(commande):
    """À la confirmation de la commande (paiement validé) : montants repris
    des frais figés dans chaque article au checkout, et de la livraison
    offerte figée dans la commande (plafonnée au net)."""
    totaux = commande.article.aggregate(
        commission=Sum("montant_commission"), frais=Sum("montant_frais_fixes"),
    )
    brut = sum((article.prix_unitaire * article.quantite for article in commande.article.all()), Decimal("0"))
    net_articles = brut - (totaux["commission"] or 0) - (totaux["frais"] or 0)
    livraison = min(commande.frais_livraison_vendeur, net_articles)
    reversement, _ = Reversement.objects.get_or_create(
        commande=commande,
        defaults={
            "boutique_id": commande.boutique_id,
            "montant_brut": brut,
            "montant_commission": totaux["commission"] or 0,
            "montant_frais_fixes": totaux["frais"] or 0,
            "montant_livraison": livraison,
            "montant_net": net_articles - livraison,
        },
    )
    return reversement


def _reste_livraison_offerte(reversement):
    """Commande livrée avec livraison offerte dont le tarif dépasse le net :
    le reste est dû par le vendeur, déduit de ses prochains reversements."""
    commande = reversement.commande
    reste = commande.frais_livraison_vendeur - reversement.montant_livraison
    if reste <= 0:
        return
    AjustementVendeur.objects.get_or_create(
        commande=commande,
        nature=AjustementVendeur.Nature.LIVRAISON_OFFERTE,
        defaults={
            "boutique_id": reversement.boutique_id,
            "montant": -reste,
            "motif": f"Livraison offerte de la commande {commande.numero_commande} : reste non couvert par la vente",
        },
    )


def _verrouiller(commande):
    return Reversement.objects.select_for_update().filter(commande=commande).first()


def ouvrir_retractation(commande, moment=None):
    """Commande livrée : le délai de rétractation commence."""
    moment = moment or timezone.now()
    with transaction.atomic():
        reversement = _verrouiller(commande)
        if reversement is None or reversement.statut != Statut.EN_ATTENTE_LIVRAISON:
            return
        reversement.date_livraison = moment
        reversement.date_disponibilite = moment + timedelta(days=settings.REVERSEMENT_DELAI_RETRACTATION_JOURS)
        reversement.statut = Statut.SUSPENDU if retour_ouvert(commande) else Statut.EN_RETRACTATION
        reversement.save(update_fields=["date_livraison", "date_disponibilite", "statut", "date_mise_a_jour"])
        # Après la livraison seulement : une commande annulée avant ne coûte
        # rien au vendeur (le reversement est annulé, rien n'est dû).
        _reste_livraison_offerte(reversement)


def annuler_reversement(commande):
    with transaction.atomic():
        reversement = _verrouiller(commande)
        if reversement is None or reversement.statut in (Statut.VERSE, Statut.EN_COURS, Statut.ANNULE):
            return
        reversement.statut = Statut.ANNULE
        reversement.save(update_fields=["statut", "date_mise_a_jour"])
        # Contestation « non reçu » fondée : la livraison offerte n'est plus
        # due par le vendeur, tant que son reste n'a pas déjà été déduit.
        supprimes, _ = AjustementVendeur.objects.filter(
            commande=commande, nature=AjustementVendeur.Nature.LIVRAISON_OFFERTE, reversement_impute__isnull=True,
        ).delete()
        if not supprimes and AjustementVendeur.objects.filter(
            commande=commande, nature=AjustementVendeur.Nature.LIVRAISON_OFFERTE,
        ).exists():
            logger_securite.warning(
                "Reversement %s annulé : reste de livraison offerte déjà déduit d'un versement (à régulariser).",
                reversement.reference,
            )


def rendre_disponibles(maintenant=None):
    """Délai de rétractation écoulé → disponible. Tâche périodique."""
    return Reversement.objects.filter(
        statut=Statut.EN_RETRACTATION, date_disponibilite__lte=maintenant or timezone.now(),
    ).update(statut=Statut.DISPONIBLE, date_mise_a_jour=timezone.now())


# =====================================================================
# RETOURS
# =====================================================================

def retour_ouvert(commande):
    from apps.retours.models import DemandeRetour

    clos = (
        DemandeRetour.Statut.REJETE, DemandeRetour.Statut.ANNULE,
        DemandeRetour.Statut.REMBOURSE, DemandeRetour.Statut.CLOTURE,
    )
    return DemandeRetour.objects.filter(commande=commande).exclude(statut__in=clos).exists()


def suspendre_reversement(commande):
    """Retour ouvert : le reversement non encore versé attend son issue."""
    with transaction.atomic():
        reversement = _verrouiller(commande)
        if reversement and reversement.statut in (Statut.EN_RETRACTATION, Statut.DISPONIBLE):
            reversement.statut = Statut.SUSPENDU
            reversement.save(update_fields=["statut", "date_mise_a_jour"])


def _reprendre(reversement):
    from apps.livraison.services import contestation_ouverte

    commande = reversement.commande
    if reversement.statut != Statut.SUSPENDU or retour_ouvert(commande) or contestation_ouverte(commande):
        return
    echu = reversement.date_disponibilite and reversement.date_disponibilite <= timezone.now()
    reversement.statut = Statut.DISPONIBLE if echu else Statut.EN_RETRACTATION


def reprendre_reversement(commande):
    """Retour clos sans remboursement : le reversement reprend son cours."""
    with transaction.atomic():
        reversement = _verrouiller(commande)
        if reversement:
            _reprendre(reversement)
            reversement.save(update_fields=["statut", "date_mise_a_jour"])


def part_vendeur_retournee(demande_retour):
    """Ce que le vendeur perd sur les articles retournés : leur prix moins
    leur commission (rendue au vendeur). Le frais fixe reste acquis à ANITCHE."""
    total = Decimal("0")
    for ligne in demande_retour.articles.select_related("commande_item"):
        article = ligne.commande_item
        commission = (article.montant_commission * ligne.quantite / article.quantite).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP,
        )
        total += article.prix_unitaire * ligne.quantite - commission
    return total


def facturer_frais_livraison_retour(demande_retour):
    """Frais de livraison rendus au client pour un retour imputable au
    vendeur (article manquant, défectueux, non conforme) : supportés par le
    vendeur, par un ajustement négatif (une fois par commande)."""
    frais = demande_retour.frais_livraison_rembourses
    if frais <= 0:
        return None
    commande = demande_retour.commande
    ajustement, _ = AjustementVendeur.objects.get_or_create(
        commande=commande,
        nature=AjustementVendeur.Nature.FRAIS_LIVRAISON_RETOUR,
        defaults={
            "boutique_id": commande.boutique_id,
            "montant": -frais,
            "motif": f"Frais de livraison remboursés au client : retour {demande_retour.numero_retour} "
                     f"({demande_retour.get_motif_display()}, commande {commande.numero_commande})",
        },
    )
    return ajustement


def appliquer_retour_rembourse(demande_retour):
    commande = demande_retour.commande
    deduction = part_vendeur_retournee(demande_retour)
    with transaction.atomic():
        reversement = _verrouiller(commande)
        if reversement is None or reversement.statut == Statut.ANNULE:
            return
        if reversement.statut in (Statut.VERSE, Statut.EN_COURS):
            if deduction > 0:
                AjustementVendeur.objects.get_or_create(
                    retour=demande_retour,
                    defaults={
                        "boutique_id": reversement.boutique_id,
                        "montant": -deduction,
                        "motif": f"Retour {demande_retour.numero_retour} remboursé après versement "
                                 f"(commande {commande.numero_commande})",
                    },
                )
            return
        reversement.montant_retours += deduction
        reversement.recalculer_net()
        _reprendre(reversement)
        reversement.save(update_fields=["montant_retours", "montant_net", "statut", "date_mise_a_jour"])


# =====================================================================
# VERSEMENT
# =====================================================================

def numero_international(numero):
    chiffres = re.sub(r"\D", "", str(numero or ""))
    if len(chiffres) == 10:
        return f"+225{chiffres}"
    if len(chiffres) == 13 and chiffres.startswith("225"):
        return f"+{chiffres}"
    raise ErreurVersement("Numéro mobile money du vendeur invalide (format ivoirien attendu).")


def operateur_du_numero(numero_e164):
    operateur = PREFIXES_OPERATEURS.get(numero_e164[4:6])
    if operateur is None:
        raise ErreurVersement("Opérateur non déductible du numéro : précisez « operateur ».")
    return operateur


def _numero_kyc(reversement):
    dossier = getattr(reversement.boutique.proprietaire, "dossier_kyc", None)
    if dossier is None or not dossier.numero_mobile_money:
        raise ErreurVersement("Le vendeur n'a pas de numéro mobile money dans son dossier KYC.")
    return str(dossier.numero_mobile_money)


def _imputer_ajustements(reversement):
    """Déduit les ajustements négatifs en attente de la boutique, du plus
    ancien au plus récent, tant que le montant versé reste positif ou nul."""
    total = reversement.montant_net
    en_attente = AjustementVendeur.objects.select_for_update().filter(
        boutique_id=reversement.boutique_id, reversement_impute__isnull=True,
    ).order_by("date_creation")
    for ajustement in en_attente:
        if total + ajustement.montant >= 0:
            ajustement.reversement_impute = reversement
            ajustement.save(update_fields=["reversement_impute"])
            total += ajustement.montant
    reversement.montant_ajustements = total - reversement.montant_net
    reversement.montant_a_verser = total


def _liberer_ajustements(reversement):
    AjustementVendeur.objects.filter(reversement_impute=reversement).update(reversement_impute=None)
    reversement.montant_ajustements = 0
    reversement.montant_a_verser = 0


def _notifier_vendeur(reversement):
    from apps.notifications import liens
    from apps.notifications.models import Notification
    from apps.notifications.services import ServiceNotification

    ServiceNotification.notifier_utilisateur(
        reversement.boutique.proprietaire,
        titre="Reversement effectué",
        message=(
            f"{int(reversement.montant_a_verser)} FCFA vous ont été versés pour la commande "
            f"{reversement.commande.numero_commande} ({reversement.reference})."
        ),
        type_notification=Notification.TypeNotification.PAIEMENT,
        lien_redirection=liens.lien_reversements_vendeur(),
        metadata={"reversement": str(reversement.pk)},
    )


def _finaliser(reversement):
    reversement.statut = Statut.VERSE
    reversement.date_versement = timezone.now()


def verser_manuellement(reversement, administrateur, reference_externe):
    """L'administration a versé elle-même (mobile money) et saisit la référence."""
    with transaction.atomic():
        verrouille = Reversement.objects.select_for_update().select_related("boutique__proprietaire").get(pk=reversement.pk)
        if verrouille.statut != Statut.DISPONIBLE:
            raise ErreurVersement("Seul un reversement disponible peut être versé.")
        _imputer_ajustements(verrouille)
        if verrouille.montant_a_verser > 0 and not reference_externe:
            raise ErreurVersement("La référence du versement est obligatoire.")
        verrouille.numero_destinataire = _numero_kyc(verrouille)
        verrouille.reference_externe = reference_externe
        verrouille.verse_par = administrateur
        verrouille.fournisseur = ""
        _finaliser(verrouille)
        verrouille.save()
    logger_securite.info("Reversement %s versé manuellement par admin_id=%s", verrouille.reference, administrateur.id)
    _notifier_vendeur(verrouille)
    return verrouille


def _revenir_disponible(reversement_id, motif):
    """Refus certain de transferer (rien n'a été créé chez le fournisseur)."""
    with transaction.atomic():
        verrouille = Reversement.objects.select_for_update().get(pk=reversement_id)
        if verrouille.statut != Statut.EN_COURS:
            return verrouille
        _liberer_ajustements(verrouille)
        verrouille.statut = Statut.DISPONIBLE
        verrouille.save()
    logger.warning("Transfert du reversement %s refusé : %s", verrouille.reference, motif)
    return verrouille


def alerter_administration_reversement(reversement, raison):
    """Journal de sécurité + notification de chaque administrateur actif
    (même mécanisme que services.alerter_administration). Jamais de numéro
    de téléphone dans le message."""
    from apps.notifications import liens
    from apps.notifications.models import Notification
    from apps.notifications.services import ServiceNotification

    message = (
        f"Reversement {reversement.reference} ({int(reversement.montant_a_verser)} FCFA à verser, "
        f"fournisseur {reversement.fournisseur or 'aucun'}) : {raison} Diagnostic : python manage.py "
        f"reconcilier_reversements --reference {reversement.reference} --lecture-seule, puis, si le "
        f"fournisseur ne tranche pas, resoudre_reversement."
    )
    logger_securite.error("ALERTE ADMINISTRATION — %s", message)
    ServiceNotification.notifier_administration(
        titre="Reversement à vérifier",
        message=message,
        type_notification=Notification.TypeNotification.PAIEMENT,
        lien_redirection=liens.lien_reversements_administration(),
        metadata={"reversement": str(reversement.pk)},
    )


@dataclass(frozen=True)
class EtatApplique:
    """Résultat de appliquer_etat_transfert."""

    reversement: Reversement
    issue: str  # SUCCES, ECHEC, EN_ATTENTE ou REFUSE
    change: bool  # le statut du reversement a changé
    motif: str = ""  # motif d'un REFUSE


def _deja_verse_par(reversement, etat):
    """Reversement versé par CE transfert : un nouveau succès n'en est que la
    confirmation répétée. Un versement manuel (fournisseur vide) ou un autre
    transfert (autre référence du fournisseur) ne l'est pas."""
    if reversement.statut != Statut.VERSE or not reversement.fournisseur:
        return False
    return (not etat.identifiant_externe or not reversement.reference_externe
            or etat.identifiant_externe == reversement.reference_externe)


def appliquer_etat_transfert(reversement_id, etat, source):
    """Seul endroit qui applique un état de transfert renvoyé par le
    fournisseur : réponse immédiate de transferer, notification vérifiée,
    réconciliation (`source` : clé de SOURCES). Sous verrou de ligne, statut
    relu : seul un reversement « en cours » change, une seule fois.

        SUCCES conforme      → verse, vendeur notifié une fois
        SUCCES non conforme  → reste en_cours (REFUSE), alerte : l'argent a pu
                               partir avec un autre montant
        ECHEC                → disponible, ajustements libérés
        EN_ATTENTE           → rien

    Conforme : montant entier égal au montant_a_verser lu sous verrou, devise
    XOF (services.controler_montant_et_devise). Un SUCCES reçu pour un
    reversement qui n'est plus en cours ne change rien et alerte (double
    paiement possible), sauf s'il confirme le transfert qui l'a versé ; un
    ECHEC ou une attente y sont sans effet. Renvoie un EtatApplique."""
    from .services import IncoherenceTransaction, controler_montant_et_devise

    issue, change, motif, alerte = etat.statut, False, "", ""
    with transaction.atomic():
        verrouille = (Reversement.objects.select_for_update().select_related("boutique__proprietaire", "commande")
                      .get(pk=reversement_id))
        if verrouille.statut != Statut.EN_COURS:
            if etat.statut == SUCCES and not _deja_verse_par(verrouille, etat):
                alerte = (
                    f"succès d'un transfert confirmé par le fournisseur (référence "
                    f"{etat.identifiant_externe or 'absente'}, {etat.montant} {etat.devise}) alors que le "
                    f"reversement est « {verrouille.get_statut_display()} » : double paiement possible."
                )
        elif etat.statut == SUCCES:
            try:
                controler_montant_et_devise(etat, verrouille.montant_a_verser, DEVISE_TRANSFERT)
            except IncoherenceTransaction as erreur:
                issue, motif = REFUSE, str(erreur)
                alerte = f"succès du transfert non appliqué, reversement laissé en cours ({erreur})."
            else:
                if not verrouille.reference_externe:
                    verrouille.reference_externe = etat.identifiant_externe
                _finaliser(verrouille)
                verrouille.save()
                change = True
        elif etat.statut == ECHEC:
            _liberer_ajustements(verrouille)
            verrouille.statut = Statut.DISPONIBLE
            verrouille.save()
            change = True

    if change and verrouille.statut == Statut.VERSE:
        logger_securite.info("Reversement %s : en_cours → verse (source=%s).", verrouille.reference, source)
        _notifier_vendeur(verrouille)
    elif change:
        logger_securite.warning("Reversement %s : en_cours → disponible, échec confirmé (source=%s).",
                                verrouille.reference, source)
    if issue == REFUSE:
        logger_securite.error("Transfert %s : succès %s non appliqué, reversement laissé en cours (%s).",
                              verrouille.reference, SOURCES[source], motif)
    elif alerte:
        logger_securite.error("Transfert %s : succès %s pour un reversement « %s », aucune transition.",
                              verrouille.reference, SOURCES[source], verrouille.statut)
    # Succès non conforme : alerte dédupliquée (la réconciliation le revoit
    # à chaque passage). Double paiement possible : toujours.
    if alerte and (issue != REFUSE or _reserver_alerte(verrouille.pk, timezone.now())):
        alerter_administration_reversement(verrouille, alerte)
    return EtatApplique(verrouille, issue, change, motif)


def transferer_reversement(reversement, administrateur, operateur=None):
    """Versement par l'API de transfert du fournisseur actif.

    Le reversement passe « en cours » avant l'appel. Seul un refus certain
    (REFUS_CERTAINS : rien n'a été créé chez le fournisseur) le rend
    disponible (ErreurVersement). Toute autre erreur du fournisseur laisse
    l'issue incertaine : le reversement reste « en cours », montant et
    ajustements figés, jusqu'à la notification ou la réconciliation. La
    réponse du fournisseur est appliquée par appliquer_etat_transfert."""
    fournisseur = fournisseur_actif()
    with transaction.atomic():
        verrouille = Reversement.objects.select_for_update().select_related("boutique__proprietaire").get(pk=reversement.pk)
        if verrouille.statut != Statut.DISPONIBLE:
            raise ErreurVersement("Seul un reversement disponible peut être versé.")
        numero = numero_international(_numero_kyc(verrouille))
        operateur = operateur or operateur_du_numero(numero)
        if operateur not in OPERATEURS:
            raise ErreurVersement(f"Opérateur inconnu : {operateur}.")
        _imputer_ajustements(verrouille)
        if verrouille.montant_a_verser <= 0:
            raise ErreurVersement("Rien à verser par transfert : utilisez le versement manuel.")
        verrouille.statut = Statut.EN_COURS
        verrouille.numero_destinataire = numero
        verrouille.operateur = operateur
        verrouille.fournisseur = fournisseur.code
        verrouille.verse_par = administrateur
        verrouille.date_debut_transfert = timezone.now()
        verrouille.date_derniere_reconciliation = None
        verrouille.date_derniere_alerte = None
        verrouille.save()

    # Appel réseau hors transaction.
    try:
        resultat = fournisseur.transferer(verrouille, numero, operateur)
    except REFUS_CERTAINS as erreur:
        _revenir_disponible(verrouille.pk, str(erreur))
        raise ErreurVersement(f"Transfert refusé : {erreur}")
    except ErreurFournisseur as erreur:
        logger_securite.error("Transfert %s : issue incertaine (%s), reversement laissé en cours jusqu'à la "
                              "confirmation du fournisseur.", verrouille.reference, erreur)
        verrouille = Reversement.objects.get(pk=verrouille.pk)
    else:
        Reversement.objects.filter(pk=verrouille.pk, statut=Statut.EN_COURS).update(
            reference_externe=resultat.identifiant_externe,
            hash_jeton_notification=hacher_jeton(resultat.jeton_notification),
        )
        verrouille = appliquer_etat_transfert(verrouille.pk, resultat, "immediat").reversement
    logger_securite.info("Transfert du reversement %s lancé par admin_id=%s (%s)",
                         verrouille.reference, administrateur.id, verrouille.statut)
    return verrouille


def traiter_notification_transfert(code, request):
    """Notification de transfert : même chaîne que pour les paiements,
    l'état vérifié est appliqué par appliquer_etat_transfert."""
    from .services import ResultatNotification, _journaliser, _ouvrir_journal

    try:
        fournisseur = obtenir_fournisseur(code)
        notification = fournisseur.lire_notification_transfert(request)
    except FournisseurInconnu:
        return ResultatNotification(404, "Fournisseur inconnu.")
    except NotificationInvalide as erreur:
        logger_securite.warning("Notification de transfert %s refusée : %s", code, erreur)
        return ResultatNotification(401, "Notification non authentique.")

    # Fournisseur vide : reversement versé à la main après un transfert
    # incertain ; un succès tardif doit encore l'atteindre (alerte).
    reversement = Reversement.objects.filter(reference=notification.reference, fournisseur__in=(code, "")).first()
    if reversement is None:
        return ResultatNotification(404, "Reversement inconnu.")
    try:
        fournisseur.authentifier_notification(notification, reversement)
    except NotificationInvalide:
        logger_securite.warning("Notification de transfert %s refusée pour %s.", code, reversement.reference)
        return ResultatNotification(401, "Notification non authentique.")

    journal, deja_traite = _ouvrir_journal(code, notification)
    if deja_traite:
        return ResultatNotification(200, "Événement déjà traité.")
    try:
        etat = fournisseur.verifier_transfert(reversement, notification)
    except (ErreurFournisseur, OperationNonSupportee) as erreur:
        _journaliser(journal, JournalWebhook.StatutTraitement.ERREUR, str(erreur))
        return ResultatNotification(503, "Vérification auprès du fournisseur impossible, réessayez.")
    except NotificationInvalide as erreur:
        _journaliser(journal, JournalWebhook.StatutTraitement.ERREUR, str(erreur))
        return ResultatNotification(400, "Notification incohérente.")

    applique = appliquer_etat_transfert(reversement.pk, etat, "notification")
    if applique.issue == REFUSE:
        _journaliser(journal, JournalWebhook.StatutTraitement.ERREUR, applique.motif)
        return ResultatNotification(400, "Transfert incohérent avec le reversement.")
    if applique.issue not in (SUCCES, ECHEC):
        _journaliser(journal, JournalWebhook.StatutTraitement.IGNORE)
        return ResultatNotification(200, "Transfert non finalisé.")
    _journaliser(journal, JournalWebhook.StatutTraitement.TRAITE)
    return ResultatNotification(200, f"Reversement {reversement.reference} traité.")


# =====================================================================
# RÉCONCILIATION DES TRANSFERTS (notification perdue, issue incertaine)
# =====================================================================

def _marquer_reconcilie(reversement):
    # update() : ni date_mise_a_jour ni aucun autre champ ne change.
    Reversement.objects.filter(pk=reversement.pk).update(date_derniere_reconciliation=timezone.now())


def reconcilier_reversement(reversement, indisponibles=None, non_disponibles=None):
    """Redemande au fournisseur l'état réel du transfert, HORS transaction
    (aucun verrou tenu pendant l'appel réseau), puis l'applique par
    appliquer_etat_transfert, sous verrou, comme une notification. Idempotent
    et sûr face à une notification ou une autre réconciliation simultanées.

    Renvoie (issue, change). issue : SUCCES, ECHEC, EN_ATTENTE, REFUSE
    (succès non conforme, réponse incohérente, fournisseur inconnu),
    INTROUVABLE, INJOIGNABLE ou NON_DISPONIBLE (pas de transferts par API
    chez ce fournisseur : aucun appel réseau). Seuls un succès conforme et un
    échec confirmé changent le statut ; un transfert introuvable reste en
    cours (un « inconnu » ne prouve pas que rien n'est parti), l'alerte par
    l'âge prend le relais. `indisponibles` / `non_disponibles` (codes
    fournisseur) évitent de rappeler, dans la même exécution, un fournisseur
    injoignable ou sans transferts par API."""
    code = reversement.fournisseur
    if indisponibles is not None and code in indisponibles:
        return INJOIGNABLE, False
    if non_disponibles is not None and code in non_disponibles:
        _marquer_reconcilie(reversement)
        return NON_DISPONIBLE, False
    try:
        etat = obtenir_fournisseur(code).verifier_transfert(reversement)
    except FournisseurInconnu:
        logger_securite.error("Réconciliation du reversement %s impossible : fournisseur %r inconnu.",
                              reversement.reference, code)
        _marquer_reconcilie(reversement)
        return REFUSE, False
    except OperationNonSupportee as erreur:
        # Daté : passe après les autres au prochain lot, sans les bloquer.
        logger.warning("Réconciliation du reversement %s : %s", reversement.reference, erreur)
        if non_disponibles is not None:
            non_disponibles.add(code)
        _marquer_reconcilie(reversement)
        return NON_DISPONIBLE, False
    except ErreurFournisseur as erreur:
        logger.warning("Réconciliation du reversement %s : fournisseur %s injoignable (%s).",
                       reversement.reference, code, erreur)
        if indisponibles is not None:
            indisponibles.add(code)
        return INJOIGNABLE, False
    except TransactionIntrouvable as erreur:
        logger_securite.error("Réconciliation du reversement %s : transfert introuvable chez %s (%s), "
                              "reversement laissé en cours.", reversement.reference, code, erreur)
        _marquer_reconcilie(reversement)
        return INTROUVABLE, False
    except NotificationInvalide as erreur:
        logger_securite.error("Réconciliation du reversement %s : réponse incohérente du fournisseur (%s).",
                              reversement.reference, erreur)
        _marquer_reconcilie(reversement)
        return REFUSE, False
    applique = appliquer_etat_transfert(reversement.pk, etat, "reconciliation")
    _marquer_reconcilie(reversement)
    return applique.issue, applique.change


def _en_cours_depuis(seuil):
    """Reversements en cours dont le transfert a commencé avant `seuil`
    (annotation `debut`). Sans date de début, la dernière mise à jour en
    tient lieu."""
    return (
        Reversement.objects.filter(statut=Statut.EN_COURS)
        .annotate(debut=Coalesce("date_debut_transfert", "date_mise_a_jour"))
        .filter(debut__lt=seuil)
    )


def reconcilier_reversements_en_cours(maintenant=None):
    """Tâche planifiée (tasks.reconcilier_reversements) : vérifie chez le
    fournisseur les reversements en cours depuis plus de
    REVERSEMENT_RECONCILIATION_AGE_MINUTES (en deçà, la notification arrive
    normalement).

    Au plus REVERSEMENT_RECONCILIATION_LOT reversements par exécution, les
    moins récemment vérifiés d'abord ; arrêt dès qu'un fournisseur est
    injoignable (un refus, un transfert introuvable ou un fournisseur sans
    transferts par API n'arrêtent rien). Renvoie (vérifiés, changés,
    interrompu)."""
    maintenant = maintenant or timezone.now()
    lot = list(
        _en_cours_depuis(maintenant - timedelta(minutes=settings.REVERSEMENT_RECONCILIATION_AGE_MINUTES))
        .order_by(F("date_derniere_reconciliation").asc(nulls_first=True), "debut")
        [:settings.REVERSEMENT_RECONCILIATION_LOT]
    )
    verifies = changes = 0
    indisponibles, non_disponibles = set(), set()
    for reversement in lot:
        issue, change = reconcilier_reversement(reversement, indisponibles, non_disponibles)
        if issue == INJOIGNABLE:
            logger.warning("Réconciliation des reversements interrompue : fournisseur %s injoignable.",
                           reversement.fournisseur)
            return verifies, changes, True
        verifies += 1
        changes += change
    return verifies, changes, False


def _reserver_alerte(reversement_id, maintenant):
    """True si une alerte peut partir pour ce reversement en cours (aucune
    depuis REPETITION_ALERTE). La date est posée par le même UPDATE
    conditionnel : deux exécutions simultanées n'alertent qu'une fois."""
    return bool(
        Reversement.objects.filter(pk=reversement_id, statut=Statut.EN_COURS)
        .filter(Q(date_derniere_alerte__isnull=True) | Q(date_derniere_alerte__lte=maintenant - REPETITION_ALERTE))
        .update(date_derniere_alerte=maintenant)
    )


def alerter_reversements_bloques(maintenant=None):
    """Alerte l'administration pour chaque reversement en cours depuis plus
    de REVERSEMENT_ALERTE_BLOCAGE_HEURES : une fois, puis toutes les 24 h
    tant qu'il le reste. Ne dépend pas du fournisseur (s'exécute même s'il
    est injoignable). Renvoie le nombre d'alertes envoyées."""
    maintenant = maintenant or timezone.now()
    bloques = (
        _en_cours_depuis(maintenant - timedelta(hours=settings.REVERSEMENT_ALERTE_BLOCAGE_HEURES))
        .filter(Q(date_derniere_alerte__isnull=True) | Q(date_derniere_alerte__lte=maintenant - REPETITION_ALERTE))
        .order_by("debut")
    )
    alertes = 0
    for reversement in bloques:
        if not _reserver_alerte(reversement.pk, maintenant):
            continue
        heures = int((maintenant - reversement.debut).total_seconds() // 3600)
        alerter_administration_reversement(
            reversement, f"transfert en cours depuis {heures} h sans confirmation du fournisseur.",
        )
        alertes += 1
    return alertes


#: Décisions de resoudre_transfert_incertain.
NON_PARTI = "non-parti"
VERSE_CONFIRME = "verse"


def resoudre_transfert_incertain(reversement, administrateur, decision, motif, reference_externe=""):
    """Sortie manuelle d'un reversement en cours que le fournisseur ne
    tranche pas (transfert introuvable, bloqué) : l'administration a vérifié
    le transfert dans le tableau de bord du fournisseur. Jamais automatique.

        non-parti → disponible, ajustements libérés : aucun argent n'est parti
        verse     → versé (référence du fournisseur obligatoire), vendeur notifié

    Administrateur actif de l'administration et motif obligatoires, journal
    de sécurité. Le fournisseur du transfert est conservé : un succès tardif
    de ce même transfert reste silencieux, un autre alerte. Lève
    ErreurVersement."""
    from apps.vendeurs.permissions import ROLES_ADMINISTRATION

    motif, reference_externe = (motif or "").strip(), (reference_externe or "").strip()
    if decision not in (NON_PARTI, VERSE_CONFIRME):
        raise ErreurVersement(f"Décision inconnue : {decision}.")
    if administrateur is None or not administrateur.is_active or administrateur.role not in ROLES_ADMINISTRATION:
        raise ErreurVersement("Seul un administrateur actif peut résoudre un transfert.")
    if not motif:
        raise ErreurVersement("Le motif est obligatoire.")
    if decision == VERSE_CONFIRME and not reference_externe:
        raise ErreurVersement("La référence du transfert chez le fournisseur est obligatoire.")
    if len(reference_externe) > Reversement._meta.get_field("reference_externe").max_length:
        raise ErreurVersement("Référence du fournisseur trop longue.")
    with transaction.atomic():
        verrouille = (Reversement.objects.select_for_update().select_related("boutique__proprietaire", "commande")
                      .get(pk=reversement.pk))
        if verrouille.statut != Statut.EN_COURS:
            raise ErreurVersement("Seul un reversement en cours peut être résolu.")
        if decision == NON_PARTI:
            _liberer_ajustements(verrouille)
            verrouille.statut = Statut.DISPONIBLE
        else:
            verrouille.reference_externe = reference_externe
            verrouille.verse_par = administrateur
            _finaliser(verrouille)
        verrouille.save()
    logger_securite.warning("Reversement %s : en_cours → %s (source=admin, admin_id=%s, décision=%s, motif=%r).",
                            verrouille.reference, verrouille.statut, administrateur.id, decision, motif)
    if verrouille.statut == Statut.VERSE:
        _notifier_vendeur(verrouille)
    return verrouille


# =====================================================================
# VUE VENDEUR
# =====================================================================

def resume_vendeur(boutique):
    """Totaux nets par étape, en une requête."""
    from django.db.models import Count, Q

    filtres = {
        "en_attente_livraison": Q(statut=Statut.EN_ATTENTE_LIVRAISON),
        "en_retractation": Q(statut__in=(Statut.EN_RETRACTATION, Statut.SUSPENDU)),
        "disponible": Q(statut__in=(Statut.DISPONIBLE, Statut.EN_COURS)),
        "verse": Q(statut=Statut.VERSE),
    }
    agregats = {}
    for cle, filtre in filtres.items():
        champ = "montant_a_verser" if cle == "verse" else "montant_net"
        agregats[cle] = Sum(champ, filter=filtre, default=0)
        agregats[f"nombre_{cle}"] = Count("id", filter=filtre)
    totaux = Reversement.objects.filter(boutique=boutique).aggregate(**agregats)
    ajustements = AjustementVendeur.objects.filter(
        boutique=boutique, reversement_impute__isnull=True,
    ).aggregate(total=Sum("montant", default=0))["total"]
    return {
        **{cle: int(valeur) for cle, valeur in totaux.items()},
        "ajustements_en_attente": int(ajustements),
        "delai_retractation_jours": settings.REVERSEMENT_DELAI_RETRACTATION_JOURS,
    }

