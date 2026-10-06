"""Registre des fournisseurs de paiement.

Le fournisseur actif est choisi par PAIEMENT_FOURNISSEUR (settings). Un
paiement garde le code du fournisseur qui l'a ouvert (Paiement.fournisseur) :
ses notifications ne sont acceptées que sur l'URL de ce fournisseur.
"""

from django.conf import settings
from django.utils.module_loading import import_string

FOURNISSEURS = {
    "simule": "apps.paiements.fournisseurs.simule.FournisseurSimule",
    "cinetpay": "apps.paiements.fournisseurs.cinetpay.FournisseurCinetPay",
}


class FournisseurInconnu(Exception):
    pass


def obtenir_fournisseur(code):
    chemin = FOURNISSEURS.get(code)
    if chemin is None:
        raise FournisseurInconnu(code)
    return import_string(chemin)()


def fournisseur_actif():
    return obtenir_fournisseur(settings.PAIEMENT_FOURNISSEUR)
