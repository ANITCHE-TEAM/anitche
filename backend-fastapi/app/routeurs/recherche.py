"""Recherche publique du catalogue (docs/MODULE_RECHERCHE.md).

- GET /recherche/produits : recherche tolérante aux accents et aux fautes,
  filtres et tri de la liste Django, pagination de 20, facettes ;
- GET /recherche/suggestions : autocomplétion, 3 caractères au moins.

Routes publiques, limitées par IP (scopes `search` et `suggestions`). Les
paramètres portent les noms de Django (GET /api/catalogue/produits/) ; une
valeur invalide reçoit un 400 au format commun, là où Django l'ignore.
"""
import math
import re
import unicodedata
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError

from app.core.errors import VALIDATION_MESSAGES, CodedHTTPException
from app.core.rate_limit import rate_limit
from app.modeles.recherche import ReponseRecherche, ReponseSuggestions
from app.services import search
from app.services.search import MAX_PAGE, MAX_PRICE, PAGE_SIZE, SearchFilters

router = APIRouter(prefix="/recherche", tags=["Recherche & Suggestions"])

PRODUCTS_PATH = f"{router.prefix}/produits"
MIN_SEARCH_LENGTH = 2
MAX_SEARCH_LENGTH = 100  # Django tronque à 100 ; ici, refus explicite
MIN_SUGGESTION_LENGTH = 3  # 2 caractères : index trigramme sans effet (mesuré)
MAX_SUGGESTION_LENGTH = 50
DEFAULT_SUGGESTION_LIMIT = 8
MAX_SUGGESTION_LIMIT = 10

# SlugField de Django (ASCII) : lettres, chiffres, « _ » et « - ».
SLUG_PATTERN = re.compile(r"^[-a-zA-Z0-9_]+$")
SLUG_MESSAGE = "Ce champ ne doit contenir que des lettres, des nombres, des tirets bas _ et des traits d'union."
CONTROL_CHARACTERS_MESSAGE = "Ce champ contient des caractères non autorisés."
PRICE_RANGE_MESSAGE = "Assurez-vous que prix_max est supérieur ou égal à prix_min."
PAGE_NOT_FOUND_MESSAGE = "Page non valide."  # texte de Django
PAGE_NOT_FOUND_CODE = "page_invalide"

Sort = Literal["pertinence", "prix_asc", "prix_desc", "date_desc", "date_asc"]


def _error(field: str, error_type: str, message: str, value, ctx: dict | None = None) -> dict:
    # Même forme qu'une erreur Pydantic : traduite par app/core/errors.py
    # (type connu) ou recopiée telle quelle (msg).
    error = {"type": error_type, "loc": ("query", field), "msg": message, "input": value}
    if ctx:
        error["ctx"] = ctx
    return error


def _clean_text(value: str | None) -> str:
    """Espaces fusionnés et composition Unicode canonique (NFC : un « é »
    saisi en deux caractères devient un seul, comme dans la base). Ni
    minuscules ni accents retirés ici : c'est catalogue_normaliser (SQL)."""
    if value is None:
        return ""
    return " ".join(unicodedata.normalize("NFC", value).split())


def _check_text(field: str, raw: str, text: str, min_length: int, errors: list) -> None:
    # Caractère NUL et autres caractères de contrôle : refusés par
    # PostgreSQL (un 503 sinon), jamais utiles dans une recherche.
    if any(unicodedata.category(char) == "Cc" for char in text):
        errors.append(_error(field, "value_error", CONTROL_CHARACTERS_MESSAGE, raw))
    elif len(text) < min_length:
        message = VALIDATION_MESSAGES["string_too_short"].format(min_length=min_length)
        errors.append(_error(field, "string_too_short", message, raw, {"min_length": min_length}))


def _slug(field: str, value: str | None, errors: list) -> str | None:
    if not value:
        return None
    if not SLUG_PATTERN.match(value):
        errors.append(_error(field, "value_error", SLUG_MESSAGE, value))
    return value


async def search_filters(
    recherche: Annotated[str | None, Query(
        max_length=MAX_SEARCH_LENGTH,
        description="Texte cherché, 2 à 100 caractères, sans tenir compte des accents ni des majuscules. "
        "Cherché dans le nom et la description des produits, et dans les noms de boutique et de catégorie.",
    )] = None,
    categorie: Annotated[str | None, Query(
        max_length=120, description="Slug ou id de catégorie, sous-catégories comprises",
    )] = None,
    boutique: Annotated[str | None, Query(
        max_length=140, description="Id (chiffres uniquement) ou slug de boutique",
    )] = None,
    prix_min: Annotated[int | None, Query(ge=0, le=MAX_PRICE, description="Prix affiché minimum, FCFA entiers")] = None,
    prix_max: Annotated[int | None, Query(ge=0, le=MAX_PRICE, description="Prix affiché maximum, FCFA entiers")] = None,
) -> SearchFilters:
    errors: list[dict] = []
    text = _clean_text(recherche)
    if text:
        _check_text("recherche", recherche, text, MIN_SEARCH_LENGTH, errors)
    category = _slug("categorie", categorie, errors)
    shop = _slug("boutique", boutique, errors)
    if prix_min is not None and prix_max is not None and prix_min > prix_max:
        errors.append(_error("prix_max", "value_error", PRICE_RANGE_MESSAGE, prix_max))
    if errors:
        raise RequestValidationError(errors)
    return SearchFilters(text=text or None, category=category, shop=shop, min_price=prix_min, max_price=prix_max)


def _page_link(request: Request, filters: SearchFilters, tri: str | None, page: int) -> str:
    """Lien absolu vers une page, comme DRF : paramètres triés, sans
    `page` pour la première. Construit avec PUBLIC_BASE_URL, jamais avec
    l'en-tête Host."""
    parameters = {
        "recherche": filters.text,
        "categorie": filters.category,
        "boutique": filters.shop,
        "prix_min": filters.min_price,
        "prix_max": filters.max_price,
        "tri": tri,
        "page": page if page > 1 else None,
    }
    query = urlencode(sorted((key, value) for key, value in parameters.items() if value is not None))
    base = request.app.state.settings.public_base_url + PRODUCTS_PATH
    return f"{base}?{query}" if query else base


@router.get(
    "/produits",
    response_model=ReponseRecherche,
    summary="Recherche, filtres, tri et facettes des produits visibles",
    dependencies=[Depends(rate_limit("search"))],
)
async def rechercher_produits(
    request: Request,
    filters: Annotated[SearchFilters, Depends(search_filters)],
    tri: Annotated[Sort | None, Query(
        description="pertinence (défaut avec recherche), date_desc (défaut sans), date_asc, prix_asc, prix_desc",
    )] = None,
    page: Annotated[int, Query(ge=1, le=MAX_PAGE, description="Page de 20 résultats, 50 au plus")] = 1,
):
    """Produits visibles dans Django (vue catalogue_produit_public), 20 par
    page. Facettes (catégories, boutiques, prix) sur tout l'ensemble filtré,
    en page 1 ; total et facettes en cache 60 s, résultats jamais."""
    state = request.app.state
    ttl = state.settings.search_cache_ttl
    sort = tri or ("pertinence" if filters.text else "date_desc")

    if page == 1:
        summary = await search.fetch_summary(state.db, state.redis, filters, ttl)
        count, facettes = summary["count"], summary["facettes"]
    else:
        count, facettes = await search.fetch_count(state.db, state.redis, filters, ttl), None

    last_page = max(1, math.ceil(count / PAGE_SIZE))
    if page > last_page:
        raise CodedHTTPException(404, PAGE_NOT_FOUND_MESSAGE, PAGE_NOT_FOUND_CODE)

    rows = await search.fetch_page(state.db, filters, sort, page)
    media_base_url = state.settings.media_base_url
    return {
        "count": count,
        "next": _page_link(request, filters, tri, page + 1) if page < last_page else None,
        "previous": _page_link(request, filters, tri, page - 1) if page > 1 else None,
        "results": [search.product_result(row, media_base_url) for row in rows],
        "facettes": facettes,
    }


@router.get(
    "/suggestions",
    response_model=ReponseSuggestions,
    summary="Autocomplétion : catégories, boutiques et produits visibles",
    dependencies=[Depends(rate_limit("suggestions"))],
)
async def obtenir_suggestions(
    request: Request,
    recherche: Annotated[str, Query(
        max_length=MAX_SUGGESTION_LENGTH, description="Début du texte saisi, 3 à 50 caractères",
    )],
    limite: Annotated[int, Query(ge=1, le=MAX_SUGGESTION_LIMIT, description="Nombre de suggestions, 10 au plus")] = (
        DEFAULT_SUGGESTION_LIMIT
    ),
):
    """Catégories actives et boutiques publiques (2 au plus chacune), puis
    produits visibles. Sans accents ; tolère une faute de frappe. En cache
    60 s."""
    errors: list[dict] = []
    text = _clean_text(recherche)
    _check_text("recherche", recherche, text, MIN_SUGGESTION_LENGTH, errors)
    if errors:
        raise RequestValidationError(errors)

    state = request.app.state
    suggestions = await search.fetch_suggestions(state.db, state.redis, text, limite, state.settings.search_cache_ttl)
    return {"requete": text, "suggestions": suggestions}
