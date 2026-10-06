"""Fournisseur simulé : conseiller par règles, sans IA ni appel réseau.

Déterministe (même demande, même réponse) : le frontend peut construire et
tester ses écrans dessus. Il classe les candidats reçus (vrai catalogue)
avec le lexique (app/services/conseiller/lexique.py) :

- pertinence : 3 par terme cherché présent dans le nom du produit, 1 par
  terme dans le nom de la boutique, 2 si la catégorie est celle d'un thème
  reconnu ;
- à pertinence égale : produit qui utilise au moins la moitié du budget,
  puis ordre de la recherche.

S'il existe des candidats pertinents, seuls ceux-là sont proposés ; sinon,
les premiers du catalogue, en le disant. Textes en gabarits factuels (nom,
catégorie, prix, budget) : jamais de certification, d'origine ni de
promesse sur un produit.
"""
from app.core.settings import Settings
from app.services.conseiller import lexique
from app.services.conseiller.fournisseurs.base import Candidat, DemandeFournisseur, FournisseurIA

AUCUN_ARTICLE = "Aucun article en stock pour {cible}{budget}. Élargissez le budget ou changez de catégorie."


def _pluriel(nombre: int, mot: str) -> str:
    return f"{nombre}\xa0{mot}{'s' if nombre > 1 else ''}"


def _budget(budget: int | None) -> str:
    return f" dans votre budget de {lexique.formater_fcfa(budget)}" if budget else ""


class _Evaluation:
    def __init__(self, candidat: Candidat, rang: int, contexte: lexique.Contexte, budget: int | None):
        nom = set(lexique.formes(candidat.nom))
        boutique = set(lexique.formes(candidat.boutique))
        categorie = lexique.normaliser(candidat.categorie or "")
        self.candidat = candidat
        self.rang = rang
        self.termes_nom = [terme for terme in contexte.termes if lexique.forme(lexique.normaliser(terme)) in nom]
        termes_boutique = [terme for terme in contexte.termes if lexique.forme(lexique.normaliser(terme)) in boutique]
        self.theme_categorie = next((theme for theme in contexte.themes if theme.categorie == categorie), None)
        self.pertinence = 3 * len(self.termes_nom) + len(termes_boutique) + (2 if self.theme_categorie else 0)
        self.utilise_budget = bool(budget) and candidat.prix * 2 >= budget

    def cle(self) -> tuple:
        return (-self.pertinence, -int(self.utilise_budget), self.rang, self.candidat.id)


def _justification(evaluation: _Evaluation, budget: int | None) -> str:
    candidat = evaluation.candidat
    if evaluation.termes_nom:
        terme = evaluation.termes_nom[0]
        debut = f"{terme[0].upper()}{terme[1:]} : {lexique.theme_du_terme(terme).argument}."
    elif evaluation.theme_categorie:
        debut = f"Rayon {candidat.categorie} : {evaluation.theme_categorie.argument}."
    elif candidat.categorie:
        debut = f"Rayon {candidat.categorie}, chez {candidat.boutique}."
    else:
        debut = f"Chez {candidat.boutique}."
    prix = lexique.formater_fcfa(candidat.prix)
    return f"{debut} {prix}, dans votre budget." if budget else f"{debut} {prix}."


def _message(demande: DemandeFournisseur, contexte: lexique.Contexte, nombre: int, pertinents: bool) -> str:
    theme = contexte.themes[0] if contexte.themes else None
    budget = _budget(demande.budget_max)
    if nombre == 0:
        return AUCUN_ARTICLE.format(cible=theme.libelle if theme else "votre demande", budget=budget)
    articles = _pluriel(nombre, "article")
    if theme and pertinents:
        return f"Pour {theme.libelle}, voici {articles}{budget or ' du catalogue'}."
    if theme:
        return f"Rien de précis pour {theme.libelle} ; voici {articles} du catalogue{budget}."
    return f"Voici {articles} du catalogue{budget}."


class FournisseurSimule(FournisseurIA):
    code = "simule"
    source = "regles"
    payant = False

    async def conseiller(self, demande: DemandeFournisseur) -> dict:
        contexte = lexique.analyser(demande.historique, demande.occasion, demande.style)
        evaluations = [
            _Evaluation(candidat, rang, contexte, demande.budget_max)
            for rang, candidat in enumerate(demande.candidats)
        ]
        pertinentes = [evaluation for evaluation in evaluations if evaluation.pertinence > 0]
        choisies = sorted(pertinentes or evaluations, key=_Evaluation.cle)[: demande.max_produits]
        conseils = [theme.conseil for theme in contexte.themes] if demande.type == "conseil" else []
        return {
            "produits": [
                {"id": evaluation.candidat.id, "justification": _justification(evaluation, demande.budget_max)}
                for evaluation in choisies
            ],
            "message": _message(demande, contexte, len(choisies), bool(pertinentes)),
            "conseils": conseils,
        }


def creer(settings: Settings) -> FournisseurSimule:
    return FournisseurSimule()
