"""Cycle de vie d'une demande de retour (docs/MODULE_RETOURS.md).

Le vendeur décide ; le client peut contester un refus devant la
modération, puis devant l'administration, qui tranche en dernier recours.

    demande ─approuver (vendeur)────────────────────────────────► approuve ─en_transit─► en_transit ─receptionner─► receptionne ─rembourser─► rembourse ─cloturer─► cloture
       │ └─rejeter (vendeur)─► refuse_vendeur ─contester─┐            ▲  │
       └─escalader (système, vendeur muet)───────────────┴► en_moderation ─approuver (modérateur)─┤  └─rejeter (vendeur, sa propre approbation)─► refuse_vendeur
                                                              └─rejeter─► refuse_moderation ─contester─► en_arbitrage ─approuver (admin)─┘
                                                                                                              └─rejeter (admin)─► rejete (définitif)
    refuse_vendeur, refuse_moderation ─clore_refus (système, délai de contestation écoulé)─► rejete
    tout statut avant l'expédition ─annuler (client)─► annule (définitif)

Le reversement au vendeur reste suspendu jusqu'à la décision définitive
(rejete, annule) ou jusqu'au remboursement. Une fois le colis réceptionné,
le retour se rembourse : aucun refus ne garde le colis et le stock
réintégrés sans rien rendre au client.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.catalogue.models import Stock
from apps.commandes.models import Commande
from apps.paiements.reversements import reprendre_reversement, suspendre_reversement
from apps.paiements.services import rembourser_retour
from apps.utilisateurs.models import Role
from apps.vendeurs.permissions import ROLES_ADMINISTRATION

from .models import DemandeRetour, RetourHistorique, RetourItem
from .signals import notifier_nouvelle_demande, notifier_relance, retour_status_change

logger = logging.getLogger(__name__)
logger_securite = logging.getLogger("securite")

Statut = DemandeRetour.Statut
Source = RetourHistorique.Source

#: Motifs imputables au vendeur : les frais de livraison sont rendus au
#: client et supportés par le vendeur (AjustementVendeur).
MOTIFS_FRAIS_REMBOURSES = (
    DemandeRetour.Motif.ARTICLE_MANQUANT,
    DemandeRetour.Motif.PRODUIT_DEFECTUEUX,
    DemandeRetour.Motif.NON_CONFORME,
)

#: Refus contestables par le client jusqu'à date_limite_contestation.
STATUTS_REFUS = (Statut.REFUSE_VENDEUR, Statut.REFUSE_MODERATION)
#: Statuts où le client peut encore renoncer à son retour.
STATUTS_ANNULABLES = (
    Statut.DEMANDE, Statut.APPROUVE, Statut.REFUSE_VENDEUR, Statut.EN_MODERATION,
    Statut.REFUSE_MODERATION, Statut.EN_ARBITRAGE,
)
#: Statuts d'arrivée qui ferment le retour sans remboursement : le
#: reversement au vendeur reprend son cours (après « rembourser », c'est
#: paiements.reversements.appliquer_retour_rembourse qui le fait).
STATUTS_QUI_CLOSENT = (Statut.REJETE, Statut.ANNULE)

#: Photos justificatives par demande, et statuts où le client peut en
#: ajouter (une contestation peut s'appuyer sur une nouvelle preuve).
PHOTOS_MAX = 5
STATUTS_PHOTOS_OUVERTS = (Statut.DEMANDE, Statut.APPROUVE, Statut.EN_TRANSIT, *STATUTS_REFUS,
                          Statut.EN_MODERATION, Statut.EN_ARBITRAGE)

#: Longueur minimale du motif d'une contestation.
MOTIF_CONTESTATION_MIN = 10

#: Rappel au vendeur qui n'a pas encore répondu, avant son échéance.
RAPPEL_VENDEUR_AVANT_ECHEANCE = timedelta(hours=24)
#: Alerte de l'administration pour un dossier sans décision de la
#: modération ou de l'arbitrage (aucune décision automatique).
ALERTE_DECISION_APRES = timedelta(hours=48)

# Contrôles propres à une règle, faits sous le verrou de la demande.
ECHEANCE_VENDEUR = "echeance_vendeur"
ECHEANCE_VENDEUR_PASSEE = "echeance_vendeur_passee"
CONTESTATION = "contestation"
CONTESTATION_PASSEE = "contestation_passee"
APPROBATION_DU_VENDEUR = "approbation_du_vendeur"


@dataclass(frozen=True)
class Regle:
    depuis: tuple
    vers: str
    acteurs: tuple
    controle: str = ""
    motif: bool = False


BOUTIQUE = (Source.VENDEUR, Source.ADMINISTRATION)

#: Action → règles (statuts de départ, statut d'arrivée, qui peut l'appliquer,
#: contrôle, motif obligatoire). Pour une action donnée, un statut de départ
#: n'appartient qu'à une seule règle. Toute combinaison absente est refusée.
TRANSITIONS = {
    "approuver": (
        Regle((Statut.DEMANDE,), Statut.APPROUVE, (Source.VENDEUR,), controle=ECHEANCE_VENDEUR),
        Regle((Statut.EN_MODERATION,), Statut.APPROUVE, (Source.MODERATION,), motif=True),
        Regle((Statut.EN_ARBITRAGE,), Statut.APPROUVE, (Source.ADMINISTRATION,), motif=True),
    ),
    "rejeter": (
        Regle((Statut.DEMANDE,), Statut.REFUSE_VENDEUR, (Source.VENDEUR,), controle=ECHEANCE_VENDEUR, motif=True),
        Regle((Statut.APPROUVE,), Statut.REFUSE_VENDEUR, (Source.VENDEUR,), controle=APPROBATION_DU_VENDEUR,
              motif=True),
        Regle((Statut.EN_MODERATION,), Statut.REFUSE_MODERATION, (Source.MODERATION,), motif=True),
        Regle((Statut.EN_ARBITRAGE,), Statut.REJETE, (Source.ADMINISTRATION,), motif=True),
    ),
    "contester": (
        Regle((Statut.REFUSE_VENDEUR,), Statut.EN_MODERATION, (Source.CLIENT,), controle=CONTESTATION, motif=True),
        Regle((Statut.REFUSE_MODERATION,), Statut.EN_ARBITRAGE, (Source.CLIENT,), controle=CONTESTATION,
              motif=True),
    ),
    "en_transit": (Regle((Statut.APPROUVE,), Statut.EN_TRANSIT, (Source.CLIENT, *BOUTIQUE)),),
    "receptionner": (Regle((Statut.APPROUVE, Statut.EN_TRANSIT), Statut.RECEPTIONNE, BOUTIQUE),),
    "rembourser": (Regle((Statut.RECEPTIONNE,), Statut.REMBOURSE, BOUTIQUE),),
    "cloturer": (Regle((Statut.REMBOURSE,), Statut.CLOTURE, BOUTIQUE),),
    "annuler": (Regle(STATUTS_ANNULABLES, Statut.ANNULE, (Source.CLIENT,)),),
    # Échéances, appliquées par la tâche périodique (appliquer_echeance).
    "escalader": (Regle((Statut.DEMANDE,), Statut.EN_MODERATION, (Source.SYSTEME,), controle=ECHEANCE_VENDEUR_PASSEE),),
    "clore_refus": (Regle(STATUTS_REFUS, Statut.REJETE, (Source.SYSTEME,), controle=CONTESTATION_PASSEE),),
}

#: Actions ouvertes aux utilisateurs (PATCH traiter/) ; les autres sont
#: réservées au système.
ACTIONS_UTILISATEUR = tuple(action for action in TRANSITIONS if action not in ("escalader", "clore_refus"))

#: Commentaire des étapes faites par le système.
COMMENTAIRES_SYSTEME = {
    "escalader": "Délai de réponse du vendeur écoulé : demande transmise à la modération.",
    "clore_refus": "Délai de contestation écoulé : refus définitif.",
}


class RetourRefuse(Exception):
    """Demande ou action refusée : message pour l'utilisateur, code HTTP."""

    def __init__(self, message, code_http=400):
        super().__init__(message)
        self.message = message
        self.code_http = code_http


# =====================================================================
# ÉLIGIBILITÉ ET MONTANT
# =====================================================================

def date_livraison_de(commande):
    """Date de remise du colis (fiche de livraison), ou None."""
    livraison = getattr(commande, "livraison", None)
    return livraison.date_livraison if livraison is not None else None


def date_limite_retour(commande):
    date_livraison = date_livraison_de(commande)
    if date_livraison is None:
        return None
    return date_livraison + timedelta(days=settings.RETOUR_DELAI_JOURS)


def verifier_eligibilite(commande, maintenant=None):
    """Seule une commande livrée, dans le délai de retour, peut faire l'objet
    d'une demande. Lève RetourRefuse sinon."""
    if commande.status != Commande.Status.LIVREE:
        raise RetourRefuse("Un retour ne peut être demandé que pour une commande livrée.")
    limite = date_limite_retour(commande)
    if limite is None:
        raise RetourRefuse("Date de livraison inconnue : contactez le support.")
    if (maintenant or timezone.now()) > limite:
        raise RetourRefuse(
            f"Le délai de retour ({settings.RETOUR_DELAI_JOURS} jours après la livraison) est écoulé."
        )


def montant_a_rembourser(commande, lignes):
    """Ce que le client a réellement payé pour les articles retournés : leur
    prix, moins leur part de la remise du coupon (au prorata du montant de
    la commande hors frais de livraison), arrondi au franc inférieur.

    `lignes` : [(CommandeItem, quantité retournée)]. La somme des
    remboursements partiels d'une commande ne dépasse jamais ce qu'elle a
    payé pour ses articles. Les frais de livraison sont traités à part
    (frais_livraison_a_rembourser).
    """
    brut_commande = sum((a.prix_unitaire * a.quantite for a in commande.article.all()), Decimal("0"))
    brut_retour = sum((article.prix_unitaire * quantite for article, quantite in lignes), Decimal("0"))
    if brut_commande <= 0:
        return Decimal("0")
    return (brut_retour * commande.montant_hors_livraison / brut_commande).quantize(
        Decimal("1"), rounding=ROUND_DOWN,
    )


def frais_livraison_a_rembourser(commande, motif):
    """Frais de livraison payés par le client, rendus en entier pour un motif
    imputable au vendeur (jamais pour un changement d'avis, une mauvaise
    taille ou un autre motif), une seule fois par commande : pas si une
    autre demande en cours ou aboutie les couvre déjà.

    À appeler sous le verrou de la commande (verrouiller_commande_du_client) :
    deux demandes simultanées ne les incluent pas toutes les deux.
    """
    if motif not in MOTIFS_FRAIS_REMBOURSES or commande.frais_livraison <= 0:
        return Decimal("0")
    deja_couverts = DemandeRetour.objects.filter(
        commande=commande, frais_livraison_rembourses__gt=0,
    ).exclude(statut__in=(Statut.REJETE, Statut.ANNULE)).exists()
    return Decimal("0") if deja_couverts else commande.frais_livraison


# =====================================================================
# CRÉATION ET PHOTOS
# =====================================================================

def verrouiller_commande_du_client(commande_id, client):
    """À appeler dans la transaction de création, AVANT la validation : deux
    demandes simultanées sur la même commande sont validées l'une après
    l'autre (quantités déjà couvertes à jour)."""
    Commande.objects.select_for_update().filter(id=commande_id, client=client).first()


def creer_demande(client, donnees):
    """`donnees` : validated_data de CreerDemandeRetourSerializer (dans la
    transaction qui a verrouillé la commande). L'échéance du vendeur est
    posée par DemandeRetour.save."""
    commande = donnees["_commande"]
    demande = DemandeRetour.objects.create(
        commande=commande,
        client=client,
        boutique=donnees["_boutique"],
        motif=donnees["motif"],
        type_resolution=donnees["type_resolution"],
        description=donnees["description"],
        montant_remboursement=donnees["_montant_remboursement"],
        frais_livraison_rembourses=donnees["_frais_livraison_rembourses"],
        statut=Statut.DEMANDE,
    )
    RetourItem.objects.bulk_create([
        RetourItem(demande_retour=demande, commande_item=article, quantite=quantite)
        for article, quantite in donnees["_validated_items"]
    ])
    _tracer(demande, "", "creer", Source.CLIENT, client, "")
    # Retour ouvert : le reversement au vendeur attend son issue.
    suspendre_reversement(commande)
    transaction.on_commit(lambda: notifier_nouvelle_demande(demande))
    return demande


def verifier_ajout_photo(demande_id, client):
    """Verrouille la demande du client et vérifie qu'une photo peut encore
    être ajoutée (statut ouvert, PHOTOS_MAX non atteint). Renvoie la demande."""
    demande = DemandeRetour.objects.select_for_update().filter(pk=demande_id, client=client).first()
    if demande is None:
        raise RetourRefuse("Demande de retour introuvable.", 404)
    if demande.statut not in STATUTS_PHOTOS_OUVERTS:
        raise RetourRefuse("Ce retour est déjà traité : plus de photo possible.")
    if demande.photos.count() >= PHOTOS_MAX:
        raise RetourRefuse(f"{PHOTOS_MAX} photos au plus par demande de retour.")
    return demande


def demandes_visibles(utilisateur):
    """Client : les siennes ; vendeur : celles de sa boutique ET les siennes
    comme acheteur ; modérateur : celles passées par la modération, toutes
    boutiques ; administration : toutes."""
    qs = DemandeRetour.objects.select_related("commande", "boutique", "client").prefetch_related(
        "articles__commande_item", "photos",
    )
    if utilisateur.role in ROLES_ADMINISTRATION:
        return qs
    visibles = Q(client=utilisateur) | Q(boutique__proprietaire=utilisateur)
    if utilisateur.role == Role.MODERATEUR:
        visibles |= Q(date_moderation__isnull=False)
    return qs.filter(visibles)


# =====================================================================
# TRAITEMENT
# =====================================================================

def qualites_de(utilisateur, demande):
    """Sources au titre desquelles `utilisateur` peut agir sur `demande`, par
    ordre de priorité ; vide pour un tiers (qui ne voit pas la demande)."""
    qualites = []
    if demande.client_id == utilisateur.pk:
        qualites.append(Source.CLIENT)
    if demande.boutique.proprietaire_id == utilisateur.pk:
        qualites.append(Source.VENDEUR)
    if utilisateur.role == Role.MODERATEUR and demande.date_moderation is not None:
        qualites.append(Source.MODERATION)
    if utilisateur.role in ROLES_ADMINISTRATION:
        qualites.append(Source.ADMINISTRATION)
    return qualites


def _derniere_approbation_par_le_vendeur(demande):
    """Une demande approuvée avant l'historique n'a pu l'être qu'au niveau de
    la boutique : elle compte comme approuvée par le vendeur."""
    source = (
        RetourHistorique.objects.filter(demande_retour=demande, nouveau_statut=Statut.APPROUVE)
        .order_by("-created_at", "-id").values_list("source", flat=True).first()
    )
    return source in (None, Source.VENDEUR)


def _controler(regle, demande, maintenant):
    if regle.controle == ECHEANCE_VENDEUR:
        if demande.date_limite_vendeur is None or maintenant > demande.date_limite_vendeur:
            raise RetourRefuse("Délai de réponse écoulé : la demande est transmise à la modération.")
    elif regle.controle == ECHEANCE_VENDEUR_PASSEE:
        if demande.date_limite_vendeur is None or maintenant <= demande.date_limite_vendeur:
            raise RetourRefuse("Le délai de réponse du vendeur court encore.")
    elif regle.controle == CONTESTATION:
        if demande.date_limite_contestation is None or maintenant > demande.date_limite_contestation:
            raise RetourRefuse("Délai de contestation écoulé : la décision est définitive.")
    elif regle.controle == CONTESTATION_PASSEE:
        if demande.date_limite_contestation is None or maintenant <= demande.date_limite_contestation:
            raise RetourRefuse("Le délai de contestation court encore.")
    elif regle.controle == APPROBATION_DU_VENDEUR:
        if not _derniere_approbation_par_le_vendeur(demande):
            raise RetourRefuse("Cette approbation a été décidée en recours : elle ne vous revient plus.", 403)


def regle_applicable(demande, qualites, action, maintenant):
    """Règle de `action` que l'appelant (ses `qualites`) peut appliquer à la
    demande dans son statut actuel, contrôles compris (le motif est vérifié
    à part). Renvoie (règle, source). Lève RetourRefuse : 403 si l'action ne
    revient pas à l'appelant (jamais, ou pas à ce stade), 400 si elle est
    invalide depuis ce statut ou si un contrôle échoue."""
    regles = TRANSITIONS.get(action)
    if regles is None:
        raise RetourRefuse("Action inconnue.")
    if not any(source in regle.acteurs for regle in regles for source in qualites):
        raise RetourRefuse("Cette action ne vous revient pas.", 403)
    for regle in regles:
        if demande.statut in regle.depuis:
            source = next((s for s in qualites if s in regle.acteurs), None)
            if source is None:
                raise RetourRefuse("Cette action ne vous revient pas à ce stade du retour.", 403)
            _controler(regle, demande, maintenant)
            return regle, source
    raise RetourRefuse(
        f"Transition invalide : l'action '{action}' n'est pas autorisée depuis le statut '{demande.statut}'."
    )


def actions_possibles(demande, utilisateur, maintenant=None):
    """Actions que `utilisateur` peut faire maintenant sur `demande`, d'après
    la même table que traiter (le frontend n'a pas à recopier la règle).
    Une action à motif obligatoire y figure : le motif reste à fournir."""
    maintenant = maintenant or timezone.now()
    qualites = qualites_de(utilisateur, demande)
    possibles = []
    for action in ACTIONS_UTILISATEUR:
        try:
            regle_applicable(demande, qualites, action, maintenant)
        except RetourRefuse:
            continue
        possibles.append(action)
    return possibles


def _motif(regle, action, reponse):
    texte = (reponse or "").strip()
    if regle.motif:
        minimum = MOTIF_CONTESTATION_MIN if action == "contester" else 1
        if len(texte) < minimum:
            precision = f", {minimum} caractères au moins" if minimum > 1 else ""
            raise RetourRefuse(f"Le motif est obligatoire (reponse{precision}).")
    return texte


def _tracer(demande, ancien_statut, action, source, auteur, commentaire):
    RetourHistorique.objects.create(
        demande_retour=demande, ancien_statut=ancien_statut, nouveau_statut=demande.statut,
        action=action, source=source, auteur=auteur, commentaire=commentaire,
    )


def _reintegrer_stock(demande):
    lignes = list(demande.articles.select_related("commande_item"))
    # Verrous dans l'ordre des clés, comme le checkout et l'annulation : pas
    # d'interblocage avec eux, quel que soit le plan choisi par PostgreSQL.
    stocks = {
        stock.variante_id: stock
        for stock in Stock.objects.select_for_update().filter(
            variante_id__in=[ligne.commande_item.variante_id for ligne in lignes]
        ).order_by("variante_id")
    }
    for ligne in lignes:
        stock = stocks.get(ligne.commande_item.variante_id)
        if stock is not None:
            stock.incrementer(ligne.quantite)


def _appliquer(demande, action, regle, source, auteur, commentaire, maintenant, restock=True):
    """Change le statut, trace l'étape et applique ses effets, dans la
    transaction de l'appelant (demande verrouillée). Renvoie l'ancien statut."""
    ancien_statut = demande.statut
    demande.statut = regle.vers
    demande.date_relance = None
    champs = {"statut", "date_relance", "date_mise_a_jour"}
    if action in ("approuver", "rejeter"):
        demande.date_traitement = maintenant
        champs.add("date_traitement")
        if source == Source.VENDEUR:
            demande.reponse_vendeur = commentaire
            champs.add("reponse_vendeur")
    if ancien_statut in STATUTS_REFUS:
        demande.date_limite_contestation = None
        champs.add("date_limite_contestation")
    if regle.vers in STATUTS_REFUS:
        demande.date_limite_contestation = maintenant + timedelta(hours=settings.RETOUR_DELAI_CONTESTATION_HEURES)
        champs.add("date_limite_contestation")
    if regle.vers == Statut.EN_MODERATION:
        demande.date_moderation = maintenant
        champs.add("date_moderation")
    if regle.vers == Statut.EN_ARBITRAGE:
        demande.date_arbitrage = maintenant
        champs.add("date_arbitrage")
    if regle.vers in (Statut.REJETE, Statut.ANNULE, Statut.CLOTURE):
        demande.date_cloture = maintenant
        champs.add("date_cloture")
    demande.save(update_fields=sorted(champs))
    _tracer(demande, ancien_statut, action, source, auteur, commentaire)

    if action == "receptionner" and restock:
        _reintegrer_stock(demande)
    elif action == "rembourser":
        # L'argent n'est rendu que par l'administration : un Remboursement
        # à traiter est créé et la part du vendeur réduite (apps.paiements).
        rembourser_retour(demande)
    if regle.vers in STATUTS_QUI_CLOSENT:
        reprendre_reversement(demande.commande)
    return ancien_statut


def _signaler(demande, ancien_statut, action, source, commentaire):
    """À appeler après le commit : notifications et fidélité."""
    retour_status_change.send(
        sender=DemandeRetour, demande_retour=demande, ancien_statut=ancien_statut,
        nouveau_statut=demande.statut, action=action, par_client=source == Source.CLIENT, source=source,
        commentaire=commentaire,
    )


def _verrouiller(demande_id, skip_locked=False):
    return (
        DemandeRetour.objects.select_for_update(of=("self",), skip_locked=skip_locked)
        .select_related("commande", "boutique")
        .filter(pk=demande_id)
        .first()
    )


def traiter(demande_id, acteur, action, reponse="", restock=True):
    """Applique `action` sous verrou : deux requêtes simultanées (double
    clic, deux onglets, la tâche des échéances) voient l'une après l'autre
    le statut à jour, et la seconde est refusée. Lève RetourRefuse
    (400 / 403 / 404)."""
    if action not in ACTIONS_UTILISATEUR:
        raise RetourRefuse("Action inconnue.")

    with transaction.atomic():
        demande = _verrouiller(demande_id)
        if demande is None:
            raise RetourRefuse("Demande de retour introuvable.", 404)
        qualites = qualites_de(acteur, demande)
        if not qualites:
            raise RetourRefuse("Demande de retour introuvable.", 404)
        maintenant = timezone.now()
        regle, source = regle_applicable(demande, qualites, action, maintenant)
        commentaire = _motif(regle, action, reponse)
        ancien_statut = _appliquer(demande, action, regle, source, acteur, commentaire, maintenant, restock)

    logger.info("Retour %s : %s → %s (acteur_id=%s)", demande.numero_retour, ancien_statut, demande.statut, acteur.pk)
    _signaler(demande, ancien_statut, action, source, commentaire)
    return demande


# =====================================================================
# ÉCHÉANCES (tâche périodique apps.retours.tasks)
# =====================================================================

def appliquer_echeance(demande_id, maintenant=None):
    """Escalade une demande sans réponse du vendeur, ou rend définitif un
    refus non contesté, par le même chemin que traiter (table, historique,
    signal). Statut et échéance revérifiés sous le verrou : sans effet si la
    demande a changé entre-temps. Une demande verrouillée par une requête en
    cours est laissée au passage suivant. Renvoie la demande changée, ou None."""
    maintenant = maintenant or timezone.now()
    with transaction.atomic():
        demande = _verrouiller(demande_id, skip_locked=True)
        if demande is None:
            return None
        action = "escalader" if demande.statut == Statut.DEMANDE else "clore_refus"
        try:
            regle, source = regle_applicable(demande, (Source.SYSTEME,), action, maintenant)
        except RetourRefuse:
            return None
        ancien_statut = _appliquer(demande, action, regle, source, None, COMMENTAIRES_SYSTEME[action], maintenant)

    logger.info("Retour %s : %s → %s (échéance)", demande.numero_retour, ancien_statut, demande.statut)
    _signaler(demande, ancien_statut, action, source, COMMENTAIRES_SYSTEME[action])
    return demande


def _relance_due(demande, maintenant):
    if demande.statut == Statut.DEMANDE:
        limite = demande.date_limite_vendeur
        return limite is not None and limite - RAPPEL_VENDEUR_AVANT_ECHEANCE <= maintenant <= limite
    if demande.statut == Statut.EN_MODERATION:
        return demande.date_moderation is not None and demande.date_moderation + ALERTE_DECISION_APRES <= maintenant
    if demande.statut == Statut.EN_ARBITRAGE:
        return demande.date_arbitrage is not None and demande.date_arbitrage + ALERTE_DECISION_APRES <= maintenant
    return False


def relancer(demande_id, maintenant=None):
    """Rappel au vendeur avant son échéance, ou alerte de l'administration
    pour un dossier sans décision en modération ou en arbitrage : une seule
    fois par statut (date_relance). Renvoie True si une relance est partie."""
    maintenant = maintenant or timezone.now()
    with transaction.atomic():
        demande = _verrouiller(demande_id, skip_locked=True)
        if demande is None or demande.date_relance is not None or not _relance_due(demande, maintenant):
            return False
        demande.date_relance = maintenant
        demande.save(update_fields=["date_relance"])
    notifier_relance(demande)
    return True


def appliquer_echeances(maintenant=None):
    """Liste figée des demandes échues, puis une transaction par demande :
    une erreur est journalisée et n'empêche pas les suivantes. Renvoie
    (escaladées, refus clos, relances)."""
    maintenant = maintenant or timezone.now()
    a_escalader = list(
        DemandeRetour.objects.filter(statut=Statut.DEMANDE, date_limite_vendeur__lt=maintenant)
        .values_list("pk", flat=True)
    )
    a_clore = list(
        DemandeRetour.objects.filter(statut__in=STATUTS_REFUS, date_limite_contestation__lt=maintenant)
        .values_list("pk", flat=True)
    )
    a_relancer = list(
        DemandeRetour.objects.filter(date_relance__isnull=True).filter(
            Q(statut=Statut.DEMANDE, date_limite_vendeur__gte=maintenant,
              date_limite_vendeur__lte=maintenant + RAPPEL_VENDEUR_AVANT_ECHEANCE)
            | Q(statut=Statut.EN_MODERATION, date_moderation__lte=maintenant - ALERTE_DECISION_APRES)
            | Q(statut=Statut.EN_ARBITRAGE, date_arbitrage__lte=maintenant - ALERTE_DECISION_APRES)
        ).values_list("pk", flat=True)
    )
    totaux = []
    for identifiants, fonction in ((a_escalader, appliquer_echeance), (a_clore, appliquer_echeance),
                                   (a_relancer, relancer)):
        faits = 0
        for demande_id in identifiants:
            try:
                faits += bool(fonction(demande_id, maintenant))
            except Exception:
                logger.exception("Échéance du retour %s non appliquée.", demande_id)
        totaux.append(faits)
    return tuple(totaux)
