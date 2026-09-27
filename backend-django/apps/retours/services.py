"""Cycle de vie d'une demande de retour (docs/MODULE_RETOURS.md).

    demande ─approuver─► approuve ─en_transit─► en_transit ─receptionner─► receptionne ─rembourser─► rembourse ─cloturer─► cloture
       │                   │  └──────────────receptionner──────────────────┘
       ├─rejeter───────────┴─► rejete        (définitif)
       └─annuler (client)──┴─► annule        (définitif)

Une fois le colis réceptionné, le retour se rembourse : un désaccord passe
par l'administration (support), jamais par un rejet qui garderait le colis
et le stock réintégrés sans rien rendre au client.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.catalogue.models import Stock
from apps.commandes.models import Commande
from apps.paiements.reversements import reprendre_reversement, suspendre_reversement
from apps.paiements.services import rembourser_retour
from apps.vendeurs.permissions import ROLES_ADMINISTRATION

from .models import DemandeRetour, RetourItem
from .signals import notifier_nouvelle_demande, retour_status_change

logger = logging.getLogger(__name__)
logger_securite = logging.getLogger("securite")

Statut = DemandeRetour.Statut

#: Motifs imputables au vendeur : les frais de livraison sont rendus au
#: client et supportés par le vendeur (AjustementVendeur).
MOTIFS_FRAIS_REMBOURSES = (
    DemandeRetour.Motif.ARTICLE_MANQUANT,
    DemandeRetour.Motif.PRODUIT_DEFECTUEUX,
    DemandeRetour.Motif.NON_CONFORME,
)

#: Photos justificatives par demande, et statuts où le client peut en ajouter.
PHOTOS_MAX = 5
STATUTS_PHOTOS_OUVERTS = (Statut.DEMANDE, Statut.APPROUVE, Statut.EN_TRANSIT)


@dataclass(frozen=True)
class Transition:
    depuis: tuple
    vers: str
    client: bool = False
    boutique: bool = True


#: Action → statuts de départ, statut d'arrivée, et qui peut la faire
#: (client de la demande ; boutique = vendeur propriétaire ou administration).
TRANSITIONS = {
    "approuver": Transition((Statut.DEMANDE,), Statut.APPROUVE),
    "rejeter": Transition((Statut.DEMANDE, Statut.APPROUVE), Statut.REJETE),
    "en_transit": Transition((Statut.APPROUVE,), Statut.EN_TRANSIT, client=True),
    "receptionner": Transition((Statut.APPROUVE, Statut.EN_TRANSIT), Statut.RECEPTIONNE),
    "rembourser": Transition((Statut.RECEPTIONNE,), Statut.REMBOURSE),
    "cloturer": Transition((Statut.REMBOURSE,), Statut.CLOTURE),
    "annuler": Transition((Statut.DEMANDE, Statut.APPROUVE), Statut.ANNULE, client=True, boutique=False),
}

#: Actions qui ferment le retour sans remboursement : le reversement au
#: vendeur reprend son cours (après « rembourser », c'est
#: paiements.reversements.appliquer_retour_rembourse qui le fait).
ACTIONS_QUI_CLOSENT = ("rejeter", "annuler")


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
    transaction qui a verrouillé la commande)."""
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
    comme acheteur ; administration : toutes."""
    from django.db.models import Q

    qs = DemandeRetour.objects.select_related("commande", "boutique", "client").prefetch_related(
        "articles__commande_item", "photos",
    )
    if utilisateur.role in ROLES_ADMINISTRATION:
        return qs
    return qs.filter(Q(client=utilisateur) | Q(boutique__proprietaire=utilisateur))


# =====================================================================
# TRAITEMENT
# =====================================================================

def peut_agir_pour_la_boutique(utilisateur, demande):
    return utilisateur.role in ROLES_ADMINISTRATION or demande.boutique.proprietaire_id == utilisateur.pk


def _reintegrer_stock(demande):
    lignes = list(demande.articles.select_related("commande_item"))
    stocks = {
        stock.variante_id: stock
        for stock in Stock.objects.select_for_update().filter(
            variante_id__in=[ligne.commande_item.variante_id for ligne in lignes]
        )
    }
    for ligne in lignes:
        stock = stocks.get(ligne.commande_item.variante_id)
        if stock is not None:
            stock.incrementer(ligne.quantite)


def traiter(demande_id, acteur, action, reponse="", restock=True):
    """Applique `action` sous verrou : deux requêtes simultanées (double
    clic, deux onglets) voient l'une après l'autre le statut à jour, et la
    seconde est refusée. Lève RetourRefuse (400 / 403 / 404)."""
    transition = TRANSITIONS.get(action)
    if transition is None:
        raise RetourRefuse("Action inconnue.")

    with transaction.atomic():
        demande = (
            DemandeRetour.objects.select_for_update(of=("self",))
            .select_related("commande", "boutique")
            .filter(pk=demande_id)
            .first()
        )
        if demande is None:
            raise RetourRefuse("Demande de retour introuvable.", 404)
        est_client = demande.client_id == acteur.pk
        pour_la_boutique = peut_agir_pour_la_boutique(acteur, demande)
        if not (est_client or pour_la_boutique):
            raise RetourRefuse("Demande de retour introuvable.", 404)
        if not ((transition.client and est_client) or (transition.boutique and pour_la_boutique)):
            raise RetourRefuse("Cette action ne vous revient pas.", 403)
        if demande.statut not in transition.depuis:
            raise RetourRefuse(
                f"Transition invalide : l'action '{action}' n'est pas autorisée depuis le statut '{demande.statut}'."
            )
        if action == "rejeter" and not reponse.strip():
            raise RetourRefuse("Le motif du rejet est obligatoire (reponse).")

        ancien_statut = demande.statut
        maintenant = timezone.now()
        demande.statut = transition.vers
        champs = ["statut", "date_mise_a_jour"]
        if action in ("approuver", "rejeter"):
            demande.reponse_vendeur = reponse.strip()
            demande.date_traitement = maintenant
            champs += ["reponse_vendeur", "date_traitement"]
        if transition.vers in (Statut.REJETE, Statut.ANNULE, Statut.CLOTURE):
            demande.date_cloture = maintenant
            champs.append("date_cloture")
        demande.save(update_fields=champs)

        if action == "receptionner" and restock:
            _reintegrer_stock(demande)
        elif action == "rembourser":
            # L'argent n'est rendu que par l'administration : un Remboursement
            # à traiter est créé et la part du vendeur réduite (apps.paiements).
            rembourser_retour(demande)
        if action in ACTIONS_QUI_CLOSENT:
            reprendre_reversement(demande.commande)

    logger.info("Retour %s : %s → %s (acteur_id=%s)", demande.numero_retour, ancien_statut, demande.statut, acteur.pk)
    retour_status_change.send(
        sender=DemandeRetour, demande_retour=demande, ancien_statut=ancien_statut,
        nouveau_statut=demande.statut, action=action, par_client=est_client and transition.client,
    )
    return demande
