"""Statistiques de lecture seule du vendeur (sa boutique) et de l'administration.

Une seule définition de « vente » pour tous les indicateurs :

- une commande COMPTE si elle est payée et non annulée (statuts
  `STATUTS_VENTE`) et que son reversement n'est pas annulé (commande
  entièrement remboursée, par exemple une livraison contestée à raison, qui
  garde le statut `livree`) ;
- elle se rattache à la période par `Commande.created_at`, jour entier en
  Africa/Abidjan ;
- ses ventes brutes sont la somme de `prix_unitaire × quantite` figés dans ses
  articles : ni frais de livraison, ni remise de coupon (supportée par
  ANITCHE) ;
- le chiffre d'affaires est ses ventes brutes moins les articles retournés
  dont le retour est remboursé. Le panier moyen et le top des produits se
  lisent sur les ventes brutes (la valeur au moment de l'achat).

Tous les montants sont des entiers XOF. Chaque indicateur est un agrégat SQL :
le nombre de requêtes ne dépend pas du nombre de commandes.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from django.db.models import Avg, Count, DecimalField, ExpressionWrapper, F, Max, Q, Sum
from django.db.models.functions import Round

from apps.avis.models import Avis, SignalementAvis
from apps.commandes.models import Commande, CommandeItem
from apps.paiements.models import Reversement
from apps.retours.models import DemandeRetour, RetourItem
from apps.utilisateurs.models import Role, Utilisateur

FUSEAU = ZoneInfo('Africa/Abidjan')
PERIODE_PAR_DEFAUT_JOURS = 30
PERIODE_MAX_JOURS = 366
TOP_PRODUITS = 5

STATUTS_VENTE = (
    Commande.Status.CONFIRMEE,
    Commande.Status.PREPARATION,
    Commande.Status.EXPEDIEE,
    Commande.Status.LIVREE,
)
#: Retours qui ont abouti à un colis renvoyé ou à un remboursement.
RETOURS_ACCEPTES = (
    DemandeRetour.Statut.APPROUVE,
    DemandeRetour.Statut.EN_TRANSIT,
    DemandeRetour.Statut.RECEPTIONNE,
    DemandeRetour.Statut.REMBOURSE,
    DemandeRetour.Statut.CLOTURE,
)
RETOURS_REMBOURSES = (DemandeRetour.Statut.REMBOURSE, DemandeRetour.Statut.CLOTURE)
#: Reversements pas encore versés ni annulés (mêmes étapes que
#: apps.paiements.reversements.resume_vendeur).
REVERSEMENTS_A_VENIR = (
    Reversement.Statut.EN_ATTENTE_LIVRAISON,
    Reversement.Statut.EN_RETRACTATION,
    Reversement.Statut.SUSPENDU,
    Reversement.Statut.DISPONIBLE,
    Reversement.Statut.EN_COURS,
)

_MONTANT = DecimalField(max_digits=18, decimal_places=2)
_VENTES_LIGNE = ExpressionWrapper(F('prix_unitaire') * F('quantite'), output_field=_MONTANT)
_VALEUR_RETOURNEE = ExpressionWrapper(F('commande_item__prix_unitaire') * F('quantite'), output_field=_MONTANT)
# Commission rendue au vendeur sur la ligne retournée, arrondie au franc par
# ligne comme apps.paiements.reversements.part_vendeur_retournee.
_COMMISSION_RENDUE = Round(ExpressionWrapper(
    F('commande_item__montant_commission') * F('quantite') / F('commande_item__quantite'),
    output_field=DecimalField(max_digits=18, decimal_places=6),
))


@dataclass(frozen=True)
class Periode:
    """Jours entiers en Africa/Abidjan, bornes incluses."""

    date_debut: date
    date_fin: date

    @property
    def debut(self):
        return datetime.combine(self.date_debut, time.min, tzinfo=FUSEAU)

    @property
    def fin_exclue(self):
        return datetime.combine(self.date_fin + timedelta(days=1), time.min, tzinfo=FUSEAU)


def aujourd_hui():
    return datetime.now(FUSEAU).date()


def periode_par_defaut(date_debut=None, date_fin=None):
    """Les 30 derniers jours, aujourd'hui compris, quand une borne manque."""
    date_fin = date_fin or aujourd_hui()
    date_debut = date_debut or date_fin - timedelta(days=PERIODE_PAR_DEFAUT_JOURS - 1)
    return Periode(date_debut, date_fin)


def xof(valeur):
    """Entier XOF (demi-franc au franc supérieur)."""
    return int(Decimal(valeur or 0).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _comptee(prefixe=''):
    return Q(**{f'{prefixe}status__in': STATUTS_VENTE}) & ~Q(
        **{f'{prefixe}reversement__statut': Reversement.Statut.ANNULE}
    )


def _dans(periode, prefixe=''):
    return Q(**{f'{prefixe}created_at__gte': periode.debut, f'{prefixe}created_at__lt': periode.fin_exclue})


def _commandes_par_statut(totaux):
    return {statut.value: totaux[f'statut_{statut.value}'] for statut in Commande.Status}


def _agregats_commandes(commandes):
    """Nombre de commandes par statut, commandes comptées et livrées comptées,
    en une requête."""
    agregats = {f'statut_{statut.value}': Count('id', filter=Q(status=statut)) for statut in Commande.Status}
    agregats['comptees'] = Count('id', filter=_comptee())
    agregats['livrees_comptees'] = Count('id', filter=_comptee() & Q(status=Commande.Status.LIVREE))
    agregats['boutiques_actives'] = Count('boutique', filter=_comptee(), distinct=True)
    return commandes.aggregate(**agregats)


def _ventes(items):
    totaux = items.aggregate(
        brutes=Sum(_VENTES_LIGNE, default=0),
        commissions=Sum('montant_commission', default=0),
        frais_fixes=Sum('montant_frais_fixes', default=0),
    )
    return {cle: Decimal(valeur) for cle, valeur in totaux.items()}


def _retours_rembourses(retours):
    totaux = retours.filter(demande_retour__statut__in=RETOURS_REMBOURSES).aggregate(
        valeur=Sum(_VALEUR_RETOURNEE, default=0),
        commission_rendue=Sum(_COMMISSION_RENDUE, default=0),
    )
    return {cle: Decimal(valeur) for cle, valeur in totaux.items()}


def _reversements(reversements):
    totaux = reversements.aggregate(
        verses=Sum('montant_a_verser', filter=Q(statut=Reversement.Statut.VERSE), default=0),
        a_venir=Sum('montant_net', filter=Q(statut__in=REVERSEMENTS_A_VENIR), default=0),
    )
    return {cle: xof(valeur) for cle, valeur in totaux.items()}


def _panier_moyen(ventes_brutes, nombre_commandes):
    if not nombre_commandes:
        return 0
    return xof(ventes_brutes / nombre_commandes)


def _pourcentage(partie, total):
    if not total:
        return 0.0
    return float((Decimal(partie) * 100 / Decimal(total)).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP))


def _top_produits(items):
    lignes = (
        items.values('variante__produit_id')
        .annotate(nom=Max('nom_produit'), quantite_vendue=Sum('quantite'), ventes_brutes=Sum(_VENTES_LIGNE))
        .order_by('-ventes_brutes', '-quantite_vendue', 'variante__produit_id')[:TOP_PRODUITS]
    )
    return [
        {
            'produit': ligne['variante__produit_id'],
            'nom': ligne['nom'],
            'quantite': ligne['quantite_vendue'],
            'ventes': xof(ligne['ventes_brutes']),
        }
        for ligne in lignes
    ]


def statistiques_vendeur(boutique, periode):
    """Indicateurs de UNE boutique. La boutique vient toujours du compte
    connecté (apps.statistiques.views), jamais d'un paramètre de la requête."""
    commandes = Commande.objects.filter(boutique=boutique).filter(_dans(periode))
    items = CommandeItem.objects.filter(commande__boutique=boutique).filter(
        _dans(periode, 'commande__'), _comptee('commande__'),
    )
    retours = RetourItem.objects.filter(commande_item__commande__boutique=boutique).filter(
        _dans(periode, 'commande_item__commande__'), _comptee('commande_item__commande__'),
    )
    totaux = _agregats_commandes(commandes)
    ventes = _ventes(items)
    rembourse = _retours_rembourses(retours)
    avec_retour = DemandeRetour.objects.filter(
        commande__in=commandes.filter(_comptee(), status=Commande.Status.LIVREE), statut__in=RETOURS_ACCEPTES,
    ).aggregate(commandes=Count('commande', distinct=True))['commandes']
    avis = Avis.objects.filter(
        boutique=boutique, statut=Avis.Statut.PUBLIE,
        date_creation__gte=periode.debut, date_creation__lt=periode.fin_exclue,
    ).aggregate(note=Avg('note'), nombre=Count('id'))
    reversements = Reversement.objects.filter(boutique=boutique).filter(_dans(periode, 'commande__'))

    return {
        'chiffre_affaires': xof(ventes['brutes'] - rembourse['valeur']),
        'montant_retours': xof(rembourse['valeur']),
        'nombre_commandes': totaux['comptees'],
        'commandes_par_statut': _commandes_par_statut(totaux),
        'panier_moyen': _panier_moyen(ventes['brutes'], totaux['comptees']),
        'top_produits': _top_produits(items),
        'avis': {
            'note_moyenne': None if avis['note'] is None else round(float(avis['note']), 2),
            'nombre': avis['nombre'],
        },
        'taux_retour': _pourcentage(avec_retour, totaux['livrees_comptees']),
        'reversements': _reversements(reversements),
    }


def statistiques_administration(periode):
    """Indicateurs de la plateforme entière."""
    commandes = Commande.objects.filter(_dans(periode))
    items = CommandeItem.objects.filter(_dans(periode, 'commande__'), _comptee('commande__'))
    retours = RetourItem.objects.filter(
        _dans(periode, 'commande_item__commande__'), _comptee('commande_item__commande__'),
    )
    totaux = _agregats_commandes(commandes)
    ventes = _ventes(items)
    rembourse = _retours_rembourses(retours)
    reversements = _reversements(Reversement.objects.filter(_dans(periode, 'commande__')))

    return {
        'volume_ventes': xof(ventes['brutes'] - rembourse['valeur']),
        'montant_retours': xof(rembourse['valeur']),
        'commissions': xof(ventes['commissions'] + ventes['frais_fixes'] - rembourse['commission_rendue']),
        'nombre_commandes': totaux['comptees'],
        'commandes_par_statut': _commandes_par_statut(totaux),
        'nouveaux_clients': Utilisateur.objects.filter(
            role=Role.CLIENT, date_creation__gte=periode.debut, date_creation__lt=periode.fin_exclue,
        ).count(),
        'vendeurs_actifs': totaux['boutiques_actives'],
        'reversements': {'en_cours': reversements['a_venir'], 'verses': reversements['verses']},
        'avis_signales_en_attente': SignalementAvis.objects.filter(statut=SignalementAvis.Statut.OUVERT).count(),
    }
