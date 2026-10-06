"""Conseiller shopping (docs/MODULE_IA.md) : orchestration commune à tous les
fournisseurs.

1. Préparation : textes du client masqués (e-mails, numéros), aucun
   identifiant d'utilisateur transmis.
2. Candidats : vrai catalogue, par la recherche publique (candidats.py).
3. Fournisseur actif (app.state.conseiller_ia) : garde-fou de budget s'il
   est payant, délai AI_TIMEOUT_SECONDS. Panne, délai dépassé, sortie
   invalide ou budget épuisé : repli sur le fournisseur simulé
   (source « regles »). Une erreur du simulé lui-même est un bogue : 500.
4. Validation de la sortie, ICI et pas dans le fournisseur : schéma,
   identifiants limités aux candidats, doublons retirés, nombre maximal,
   textes nettoyés (contrôles, liens), message remplacé s'il est douteux.
5. Réponse : lignes du catalogue telles que la recherche les renvoie, plus
   la justification. Un fournisseur ne peut ni inventer un produit ni
   changer un prix.

Journaux : jamais le contenu des messages ni la réponse.
"""
import asyncio
import logging
import re
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError
from redis.exceptions import RedisError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import CodedHTTPException
from app.modeles.conseiller_ia import DemandeConseil, DemandeRecommandations
from app.services import search
from app.services.conseiller import candidats, lexique
from app.services.conseiller.fournisseurs.base import (
    Candidat,
    DemandeFournisseur,
    ErreurFournisseurIA,
    FournisseurIA,
    SortieFournisseur,
)
from app.services.conseiller.fournisseurs.simule import FournisseurSimule

logger = logging.getLogger("anitche.fastapi.conseiller")

MAX_PRODUITS_CONSEIL = 4
MAX_PRODUITS_RECOMMANDATIONS = 8
MAX_JUSTIFICATION = 300
MAX_MESSAGE = 600
MAX_CONSEILS = 3
MAX_CONSEIL = 200
JUSTIFICATION_PAR_DEFAUT = "Correspond à votre demande."

DISABLED_CODE = "conseiller_desactive"
DISABLED_MESSAGE = "Le conseiller n'est pas disponible."
UNAVAILABLE_CODE = "conseiller_indisponible"
UNAVAILABLE_MESSAGE = "Le conseiller est momentanément indisponible. Réessayez plus tard."

# Garde-fou de budget : appels payants du jour (UTC), tous utilisateurs.
BUDGET_KEY_PREFIX = "fastapi:ia:appels:"
BUDGET_KEY_TTL = 2 * 86400

MASQUE = "[masqué]"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# 8 chiffres ou plus, séparés ou non par des espaces, points ou tirets.
TELEPHONE = re.compile(r"\+?\d(?:[\s.\-]?\d){7,}")
LIEN = re.compile(r"(?i)\b(?:https?://|www\.)\S*")
# Blancs fusionnés, sauf les espaces insécables des montants (« 30 000 FCFA »).
BLANCS = re.compile(r"[^\S  ]+")


class SortieInvalide(Exception):
    """Sortie d'un fournisseur hors de la forme SortieFournisseur."""


@dataclass(frozen=True)
class SortieValidee:
    produits: tuple[tuple[int, str], ...]  # (id, justification), ordre du fournisseur
    message: str  # vide : à remplacer par le gabarit
    conseils: tuple[str, ...]


@dataclass(frozen=True)
class Resultat:
    sortie: SortieValidee
    source: str


# ------------------------------------------------------------ textes


def masquer(texte: str) -> str:
    """E-mails et numéros de téléphone remplacés avant tout fournisseur. Un
    nom propre saisi par le client reste (limite connue)."""
    return TELEPHONE.sub(MASQUE, EMAIL.sub(MASQUE, texte))


def nettoyer(texte: str, limite: int) -> tuple[str, bool]:
    """Texte d'un fournisseur prêt à afficher en texte brut : liens retirés,
    caractères de contrôle et de format (dont les inversions de sens)
    remplacés, espaces fusionnés, longueur bornée. Renvoie (texte, lien retiré)."""
    lien = LIEN.search(texte) is not None
    texte = LIEN.sub(" ", texte)
    texte = "".join(" " if unicodedata.category(char).startswith("C") else char for char in texte)
    texte = BLANCS.sub(" ", texte).strip()
    return texte[:limite].rstrip(), lien


def valider_sortie(brute, identifiants: set[int], max_produits: int) -> SortieValidee:
    try:
        if isinstance(brute, (str, bytes)):
            sortie = SortieFournisseur.model_validate_json(brute)
        elif isinstance(brute, Mapping):
            sortie = SortieFournisseur.model_validate(dict(brute))
        else:
            raise SortieInvalide(type(brute).__name__)
    except ValidationError:
        raise SortieInvalide("schéma") from None

    produits: list[tuple[int, str]] = []
    rejete = False
    for choix in sortie.produits:
        if choix.id not in identifiants:
            rejete = True
            continue
        if any(choix.id == identifiant for identifiant, _ in produits):
            continue
        justification, _ = nettoyer(choix.justification, MAX_JUSTIFICATION)
        produits.append((choix.id, justification or JUSTIFICATION_PAR_DEFAUT))
        if len(produits) == max_produits:
            break

    message, lien = nettoyer(sortie.message, MAX_MESSAGE)
    # Un message qui accompagnait un produit inventé peut le citer ; un lien
    # n'a rien à faire dans une réponse : gabarit du service à la place.
    if rejete or lien:
        message = ""
    conseils = tuple(conseil for conseil in (nettoyer(texte, MAX_CONSEIL)[0] for texte in sortie.conseils) if conseil)
    return SortieValidee(produits=tuple(produits), message=message, conseils=conseils[:MAX_CONSEILS])


def message_gabarit(nombre: int, budget: int | None) -> str:
    dans_budget = f" dans votre budget de {lexique.formater_fcfa(budget)}" if budget else ""
    if nombre == 0:
        return f"Aucun article en stock pour votre demande{dans_budget}. Élargissez le budget ou changez de catégorie."
    pluriel = "s" if nombre > 1 else ""
    return f"Voici {nombre}\xa0article{pluriel} sélectionné{pluriel} pour vous{dans_budget}."


# ------------------------------------------------------------ fournisseur


async def reserver_appel(redis, limite: int) -> bool:
    """Compte un appel payant du jour ; faux au-delà de la limite ou si le
    compteur est indisponible (jamais d'appel payant sans compteur)."""
    if limite == 0:
        return False
    cle = BUDGET_KEY_PREFIX + datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(cle)
            pipe.expire(cle, BUDGET_KEY_TTL)
            nombre, _ = await pipe.execute()
    except (RedisError, OSError) as exc:
        logger.error("Garde-fou de budget indisponible (Redis) : %s", type(exc).__name__)
        return False
    return nombre <= limite


async def _appeler(fournisseur: FournisseurIA, demande: DemandeFournisseur, delai: float) -> Resultat:
    async with asyncio.timeout(delai):
        brute = await fournisseur.conseiller(demande)
    identifiants = {candidat.id for candidat in demande.candidats}
    return Resultat(valider_sortie(brute, identifiants, demande.max_produits), fournisseur.source)


async def _executer(state, demande: DemandeFournisseur) -> Resultat:
    fournisseur: FournisseurIA = state.conseiller_ia
    settings = state.settings
    if isinstance(fournisseur, FournisseurSimule):
        return await _appeler(fournisseur, demande, settings.ai_timeout_seconds)

    debut = time.perf_counter()
    if fournisseur.payant and not await reserver_appel(state.redis, settings.ai_daily_call_limit):
        cause = "budget"
    else:
        try:
            return await _appeler(fournisseur, demande, settings.ai_timeout_seconds)
        except TimeoutError:
            cause = "delai"
        except ErreurFournisseurIA:
            cause = "erreur"
        except SortieInvalide:
            cause = "sortie_invalide"
        except Exception:
            # Bogue d'un adaptateur : le conseiller reste disponible.
            logger.exception("Erreur inattendue du fournisseur %s", fournisseur.code)
            cause = "exception"
    logger.warning(
        "Repli sur le fournisseur simulé (fournisseur=%s, cause=%s, %.0f ms)",
        fournisseur.code, cause, (time.perf_counter() - debut) * 1000,
    )
    return await _appeler(FournisseurSimule(), demande, settings.ai_timeout_seconds)


# ------------------------------------------------------------ routes


async def _candidats(state, termes, categories, budget_max) -> list:
    try:
        return await candidats.rechercher(state.db, termes=termes, categories=categories, budget_max=budget_max)
    except StarletteHTTPException as exc:
        if exc.status_code == 503:
            # PostgreSQL indisponible : aucun conseil possible sans catalogue.
            raise CodedHTTPException(503, UNAVAILABLE_MESSAGE, UNAVAILABLE_CODE) from None
        raise


def _candidat(row) -> Candidat:
    prix = row["prix_min"] if row["prix_min"] is not None else row["prix_base"]
    return Candidat(id=row["id"], nom=row["nom"], categorie=row["categorie_nom"], boutique=row["boutique_nom"], prix=int(prix))


def _produits(state, resultat: Resultat, lignes: list) -> list[dict]:
    par_id = {row["id"]: row for row in lignes}
    media_base_url = state.settings.media_base_url
    return [
        {**search.product_result(par_id[identifiant], media_base_url), "justification": justification}
        for identifiant, justification in resultat.sortie.produits
    ]


async def conseiller(state, demande: DemandeConseil) -> dict:
    historique = tuple((message.role, masquer(message.contenu)) for message in demande.messages)
    occasion = masquer(demande.occasion) if demande.occasion else None
    style = masquer(demande.style) if demande.style else None
    contexte = lexique.analyser(historique, occasion, style)
    lignes = await _candidats(state, contexte.termes, demande.categories, demande.budget_max)

    resultat = await _executer(state, DemandeFournisseur(
        type="conseil",
        historique=historique,
        occasion=occasion,
        style=style,
        budget_max=demande.budget_max,
        categories=tuple(demande.categories),
        candidats=tuple(_candidat(row) for row in lignes),
        max_produits=MAX_PRODUITS_CONSEIL,
    ))
    produits = _produits(state, resultat, lignes)
    return {
        "reponse": resultat.sortie.message or message_gabarit(len(produits), demande.budget_max),
        "produits_suggeres": produits,
        "conseils_style": list(resultat.sortie.conseils),
        "source": resultat.source,
    }


async def recommander(state, demande: DemandeRecommandations) -> dict:
    lignes = await _candidats(state, (), demande.categories, demande.budget_max)
    resultat = await _executer(state, DemandeFournisseur(
        type="recommandations",
        historique=(),
        occasion=None,
        style=None,
        budget_max=demande.budget_max,
        categories=tuple(demande.categories),
        candidats=tuple(_candidat(row) for row in lignes),
        max_produits=MAX_PRODUITS_RECOMMANDATIONS,
    ))
    return {"recommandations": _produits(state, resultat, lignes), "source": resultat.source}
