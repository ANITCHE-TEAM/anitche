"""Registre des fournisseurs du conseiller IA.

Le fournisseur actif est choisi par AI_PROVIDER (app/core/settings.py) et
créé une seule fois, au démarrage (create_app) : une valeur inconnue
empêche le service de démarrer, avec la liste des valeurs possibles.

Chaque entrée : module qui expose `creer(settings) -> FournisseurIA`,
importé seulement s'il est choisi. Ajouter un fournisseur : son module,
une ligne ici, AI_PROVIDER (docs/MODULE_IA.md, « Ajouter un fournisseur »).
"""
from importlib import import_module

from app.core.settings import Settings
from app.services.conseiller.fournisseurs.base import FournisseurIA

FOURNISSEURS = {
    "simule": "app.services.conseiller.fournisseurs.simule",
}


class FournisseurIAInconnu(Exception):
    pass


def creer_fournisseur(settings: Settings) -> FournisseurIA:
    code = settings.ai_provider
    chemin = FOURNISSEURS.get(code)
    if chemin is None:
        disponibles = ", ".join(sorted(FOURNISSEURS))
        raise FournisseurIAInconnu(f"AI_PROVIDER inconnu : « {code} » (valeurs possibles : {disponibles})")
    fournisseur = import_module(chemin).creer(settings)
    if not isinstance(fournisseur, FournisseurIA) or fournisseur.code != code:
        raise FournisseurIAInconnu(f"{chemin}.creer() doit renvoyer un FournisseurIA de code « {code} »")
    return fournisseur
