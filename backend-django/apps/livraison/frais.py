"""Frais de livraison : zone et tarif d'une commune, frais figés par commande.

Une commande = un colis = des frais : un checkout de plusieurs boutiques
paie des frais pour chaque commande. Le tarif dépend de la commune
(TarifLivraison, réglable par l'administration) ; le poids n'est pas
utilisé au lancement.

La zone n'est jamais déclarée par le client : elle est déduite de la
commune. Les communes du district d'Abidjan sont les tarifs de commune de
la zone « abidjan » (actifs ou non) ; toute autre commune est hors Abidjan.

    commune du district   tarif de la commune s'il est actif, sinon défaut d'Abidjan
    autre commune         tarif de la commune (hors Abidjan) s'il est actif, sinon
                          défaut hors Abidjan

    livraison payée par le client  frais_livraison = tarif, frais_livraison_vendeur = 0
    livraison offerte par la boutique  frais_livraison = 0, frais_livraison_vendeur = tarif
                                       (déduit du reversement du vendeur)

L'argent des frais revient à ANITCHE : jamais dans le reversement du
vendeur, aucune commission dessus.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Q

from .models import TarifLivraison, normaliser_commune

Zone = TarifLivraison.Zone


class TarifIntrouvable(Exception):
    """Aucun tarif actif pour la zone (configuration)."""


@dataclass
class TarifApplicable:
    zone: str
    tarif: TarifLivraison


def tarif_applicable(commune):
    """Zone déduite de la commune et tarif à appliquer (une requête)."""
    cle = normaliser_commune(commune)
    filtre = Q(commune_normalisee="") | Q(commune_normalisee=cle) if cle else Q(commune_normalisee="")
    ligne_commune = None
    defauts = {}
    for ligne in TarifLivraison.objects.filter(filtre):
        if ligne.commune_normalisee:
            ligne_commune = ligne
        else:
            defauts[ligne.zone] = ligne
    zone = ligne_commune.zone if ligne_commune is not None else Zone.HORS_ABIDJAN
    if ligne_commune is not None and ligne_commune.est_actif:
        return TarifApplicable(zone=zone, tarif=ligne_commune)
    defaut = defauts.get(zone)
    if defaut is None or not defaut.est_actif:
        raise TarifIntrouvable(f"Aucun tarif de livraison actif pour la zone « {zone} ».")
    return TarifApplicable(zone=zone, tarif=defaut)


def grille_publique():
    """Communes proposées au checkout (menu déroulant) avec le tarif réellement
    appliqué à chacune, et le tarif des autres villes (hors Abidjan)."""
    lignes = list(TarifLivraison.objects.all())
    defauts = {ligne.zone: ligne for ligne in lignes if not ligne.commune_normalisee and ligne.est_actif}
    communes = []
    for ligne in lignes:
        if not ligne.commune_normalisee:
            continue
        tarif = ligne if ligne.est_actif else defauts.get(ligne.zone)
        # Commune hors Abidjan inactive : couverte par « autres villes ».
        if tarif is None or (not ligne.est_actif and ligne.zone == Zone.HORS_ABIDJAN):
            continue
        communes.append({"commune": ligne.commune, "zone": ligne.zone, "montant": tarif.montant})
    autres = defauts.get(Zone.HORS_ABIDJAN)
    return {
        "communes": communes,
        "autres_villes": {"zone": Zone.HORS_ABIDJAN, "montant": autres.montant} if autres else None,
    }


def frais_de_la_commande(boutique, tarif):
    """Valeurs à figer dans la Commande."""
    montant = Decimal(tarif.montant)
    if boutique.livraison_offerte:
        return {"frais_livraison": Decimal("0"), "livraison_offerte": True, "frais_livraison_vendeur": montant}
    return {"frais_livraison": montant, "livraison_offerte": False, "frais_livraison_vendeur": Decimal("0")}
