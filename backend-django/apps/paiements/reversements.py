"""Reversements aux vendeurs : ANITCHE encaisse, puis reverse après
livraison confirmée et délai de rétractation, frais déduits.

    en_attente_livraison → en_retractation   commande livrée (date_disponibilite = +N jours)
    en_retractation      → disponible        délai écoulé (tâche périodique)
    en_retractation / disponible → suspendu  retour ouvert
    suspendu             → en_retractation / disponible   retour clos
    disponible           → en_cours → verse  transfert par le fournisseur
    disponible           → verse             versement manuel (référence saisie)
    en_cours             → disponible        transfert échoué
    tout sauf verse      → annule            commande annulée

Canal : mobile money uniquement, sur le numéro du dossier KYC du vendeur.
"""

import logging
import re
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .fournisseurs import FournisseurInconnu, fournisseur_actif, obtenir_fournisseur
from .fournisseurs.base import (
    ECHEC,
    SUCCES,
    ErreurFournisseur,
    NotificationInvalide,
    OperationNonSupportee,
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


class ErreurVersement(Exception):
    """Versement impossible (statut, numéro, montant) — message pour l'admin."""


# =====================================================================
# CYCLE DE VIE
# =====================================================================

def creer_reversement(commande):
    """À la confirmation de la commande (paiement validé) : montants repris
    des frais figés dans chaque article au checkout."""
    totaux = commande.article.aggregate(
        commission=Sum("montant_commission"), frais=Sum("montant_frais_fixes"),
    )
    brut = sum((article.prix_unitaire * article.quantite for article in commande.article.all()), Decimal("0"))
    reversement, _ = Reversement.objects.get_or_create(
        commande=commande,
        defaults={
            "boutique_id": commande.boutique_id,
            "montant_brut": brut,
            "montant_commission": totaux["commission"] or 0,
            "montant_frais_fixes": totaux["frais"] or 0,
            "montant_net": brut - (totaux["commission"] or 0) - (totaux["frais"] or 0),
        },
    )
    return reversement


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


def annuler_reversement(commande):
    with transaction.atomic():
        reversement = _verrouiller(commande)
        if reversement is None or reversement.statut in (Statut.VERSE, Statut.EN_COURS, Statut.ANNULE):
            return
        reversement.statut = Statut.ANNULE
        reversement.save(update_fields=["statut", "date_mise_a_jour"])


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

    clos = (DemandeRetour.Statut.REJETE, DemandeRetour.Statut.REMBOURSE, DemandeRetour.Statut.CLOTURE)
    return DemandeRetour.objects.filter(commande=commande).exclude(statut__in=clos).exists()


def suspendre_reversement(commande):
    """Retour ouvert : le reversement non encore versé attend son issue."""
    with transaction.atomic():
        reversement = _verrouiller(commande)
        if reversement and reversement.statut in (Statut.EN_RETRACTATION, Statut.DISPONIBLE):
            reversement.statut = Statut.SUSPENDU
            reversement.save(update_fields=["statut", "date_mise_a_jour"])


def _reprendre(reversement):
    if reversement.statut != Statut.SUSPENDU or retour_ouvert(reversement.commande):
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
    with transaction.atomic():
        verrouille = Reversement.objects.select_for_update().get(pk=reversement_id)
        if verrouille.statut != Statut.EN_COURS:
            return verrouille
        _liberer_ajustements(verrouille)
        verrouille.statut = Statut.DISPONIBLE
        verrouille.save()
    logger.warning("Transfert du reversement %s échoué : %s", verrouille.reference, motif)
    return verrouille


def transferer_reversement(reversement, administrateur, operateur=None):
    """Versement par l'API de transfert du fournisseur actif."""
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
        verrouille.save()

    # Appel réseau hors transaction.
    try:
        resultat = fournisseur.transferer(verrouille, numero, operateur)
    except (ErreurFournisseur, OperationNonSupportee) as erreur:
        _revenir_disponible(verrouille.pk, str(erreur))
        raise ErreurVersement(f"Transfert refusé : {erreur}")

    with transaction.atomic():
        verrouille = Reversement.objects.select_for_update().select_related("boutique__proprietaire").get(pk=verrouille.pk)
        verrouille.reference_externe = resultat.identifiant_externe
        verrouille.hash_jeton_notification = hacher_jeton(resultat.jeton_notification)
        if resultat.statut == SUCCES and verrouille.statut == Statut.EN_COURS:
            _finaliser(verrouille)
        verrouille.save()
    if resultat.statut == ECHEC:
        verrouille = _revenir_disponible(verrouille.pk, "refus immédiat du fournisseur")
    elif verrouille.statut == Statut.VERSE:
        _notifier_vendeur(verrouille)
    logger_securite.info("Transfert du reversement %s lancé par admin_id=%s (%s)",
                         verrouille.reference, administrateur.id, verrouille.statut)
    return verrouille


def traiter_notification_transfert(code, request):
    """Notification de transfert : même chaîne que pour les paiements."""
    from .services import ResultatNotification, _journaliser, _ouvrir_journal

    try:
        fournisseur = obtenir_fournisseur(code)
        notification = fournisseur.lire_notification_transfert(request)
    except FournisseurInconnu:
        return ResultatNotification(404, "Fournisseur inconnu.")
    except NotificationInvalide as erreur:
        logger_securite.warning("Notification de transfert %s refusée : %s", code, erreur)
        return ResultatNotification(401, "Notification non authentique.")

    reversement = Reversement.objects.filter(reference=notification.reference, fournisseur=code).first()
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

    if etat.statut == SUCCES and etat.montant is not None and etat.montant != int(reversement.montant_a_verser):
        _journaliser(journal, JournalWebhook.StatutTraitement.ERREUR, "Montant transféré différent du montant à verser.")
        logger_securite.error("Transfert %s : montant confirmé %s ≠ %s.", reversement.reference, etat.montant,
                              int(reversement.montant_a_verser))
        return ResultatNotification(400, "Transfert incohérent avec le reversement.")
    if etat.statut == SUCCES:
        with transaction.atomic():
            verrouille = Reversement.objects.select_for_update().select_related("boutique__proprietaire").get(pk=reversement.pk)
            vient_d_etre_verse = verrouille.statut == Statut.EN_COURS
            if vient_d_etre_verse:
                _finaliser(verrouille)
                verrouille.save()
        if vient_d_etre_verse:
            _notifier_vendeur(verrouille)
    elif etat.statut == ECHEC:
        _revenir_disponible(reversement.pk, "échec confirmé par le fournisseur")
    else:
        _journaliser(journal, JournalWebhook.StatutTraitement.IGNORE)
        return ResultatNotification(200, "Transfert non finalisé.")
    _journaliser(journal, JournalWebhook.StatutTraitement.TRAITE)
    return ResultatNotification(200, f"Reversement {reversement.reference} traité.")


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

