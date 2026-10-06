"""Lexique du conseiller : thèmes reconnus dans la demande du client et mots
cherchés dans le catalogue (docs/MODULE_IA.md, « Lexique »).

Utilisé par le service (quels produits chercher) et par le fournisseur
simulé (classement, textes). Ce sont des DONNÉES, à ajuster sur le vrai
catalogue ; la logique ne dépend d'aucun mot en particulier.

Comparaison sur des mots entiers, sans accents ni majuscules, au singulier
approché (« s » ou « x » final retiré) : « besoin » ne déclenche pas
« soin », « cérémonie » et « ceremonie » sont le même mot.
"""
import unicodedata
from dataclasses import dataclass

MAX_TERMES = 4
MAX_THEMES = 2
# Textes du client lus : les 3 derniers messages « user », occasion, style.
MESSAGES_LUS = 3


def normaliser(texte: str) -> str:
    """Minuscules, sans accents, lettres et chiffres seulement."""
    decompose = unicodedata.normalize("NFKD", texte.casefold())
    sans_accents = "".join(char for char in decompose if not unicodedata.combining(char))
    return " ".join("".join(char if char.isalnum() else " " for char in sans_accents).split())


def forme(mot: str) -> str:
    """Singulier approché d'un mot normalisé."""
    return mot[:-1] if len(mot) > 3 and mot[-1] in "sx" else mot


def formes(texte: str) -> list[str]:
    return [forme(mot) for mot in normaliser(texte).split()]


def formater_fcfa(montant: int) -> str:
    """30000 -> « 30 000 FCFA » (espaces insécables)."""
    return f"{montant:,}".replace(",", "\xa0") + "\xa0FCFA"


@dataclass(frozen=True)
class Theme:
    code: str
    libelle: str  # après « pour » : « Pour une cérémonie, … »
    declencheurs: tuple[str, ...]
    termes: tuple[str, ...]  # cherchés dans le catalogue (forme affichée, accents compris)
    categorie: str  # nom de catégorie favorisé, normalisé
    argument: str  # justification d'un produit trouvé par un terme du thème
    conseil: str  # conseil général, sans promesse sur un produit


THEMES = (
    Theme(
        code="ceremonie",
        libelle="une cérémonie",
        declencheurs=("mariage", "dot", "bapteme", "ceremonie", "fete", "gala", "soiree", "fiancailles", "chic"),
        termes=("bazin", "pagne", "kita", "boubou"),
        categorie="mode",
        argument="une valeur sûre pour une cérémonie",
        conseil="Pour une cérémonie, un tissu noble (bazin, kita) et une parure sobre font souvent l'unanimité.",
    ),
    Theme(
        code="bureau",
        libelle="le bureau",
        declencheurs=("bureau", "travail", "reunion", "entretien"),
        termes=("chemise", "wax", "pantalon"),
        categorie="mode",
        argument="pratique pour le bureau",
        conseil="Au bureau, une chemise en wax cintrée se porte bien avec un pantalon uni.",
    ),
    Theme(
        code="maison",
        libelle="la maison ou un cadeau",
        declencheurs=("cadeau", "maison", "deco", "decoration", "cuisine", "souvenir"),
        termes=("nappe", "mortier", "marmite"),
        categorie="maison",
        argument="une belle idée pour la maison ou à offrir",
        conseil="Pour un cadeau, un objet utile et artisanal fait toujours plaisir.",
    ),
    Theme(
        code="beaute",
        libelle="vos soins",
        declencheurs=("beaute", "soin", "peau", "cheveux", "karite", "savon", "cosmetique"),
        termes=("karité", "savon", "huile"),
        categorie="beaute",
        argument="un soin naturel du quotidien",
        conseil="Pour la peau et les cheveux, des soins simples et sans parfum conviennent au plus grand nombre.",
    ),
    Theme(
        code="electronique",
        libelle="l'électronique",
        declencheurs=("telephone", "smartphone", "portable", "ecouteur", "batterie", "chargeur"),
        termes=("smartphone", "écouteurs", "batterie"),
        categorie="electronique",
        argument="un équipement utile au quotidien",
        conseil="Avant d'acheter un accessoire électronique, vérifiez sa compatibilité avec votre appareil.",
    ),
)

# Forme de chaque déclencheur et de chaque terme, calculée une fois.
_DECLENCHEURS = {theme.code: frozenset(forme(normaliser(mot)) for mot in theme.declencheurs) for theme in THEMES}
# Terme (forme) -> (terme affiché, thème).
TERMES = {
    forme(normaliser(terme)): (terme, theme)
    for theme in reversed(THEMES)
    for terme in theme.termes
}


@dataclass(frozen=True)
class Contexte:
    themes: tuple[Theme, ...]  # dans l'ordre d'apparition, 2 au plus
    termes: tuple[str, ...]  # formes affichées, 4 au plus : mots de produit cités, puis termes des thèmes


def analyser(historique, occasion: str | None = None, style: str | None = None) -> Contexte:
    """Thèmes et termes d'une demande. `historique` : paires (rôle, texte) ;
    seuls les messages du client sont lus (les messages « assistant » sont
    forgeables et n'apportent rien au classement)."""
    textes = [texte for role, texte in historique if role == "user"][-MESSAGES_LUS:]
    textes += [occasion or "", style or ""]
    mots = [mot for texte in textes for mot in formes(texte)]

    premiere_occurrence = {}
    for position, mot in enumerate(mots):
        for theme in THEMES:
            if mot in _DECLENCHEURS[theme.code]:
                premiere_occurrence.setdefault(theme.code, position)
    themes = tuple(sorted(
        (theme for theme in THEMES if theme.code in premiere_occurrence),
        key=lambda theme: premiere_occurrence[theme.code],
    ))[:MAX_THEMES]

    cites = [TERMES[mot][0] for mot in mots if mot in TERMES]
    termes = tuple(dict.fromkeys([*cites, *(terme for theme in themes for terme in theme.termes)]))
    return Contexte(themes=themes, termes=termes[:MAX_TERMES])


def theme_du_terme(terme: str) -> Theme:
    return TERMES[forme(normaliser(terme))][1]
