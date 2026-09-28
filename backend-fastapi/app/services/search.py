"""Recherche publique du catalogue (docs/MODULE_RECHERCHE.md).

Lit UNIQUEMENT les trois vues créées par la migration Django catalogue 0004 :
catalogue_produit_public, catalogue_categorie_publique et
catalogue_boutique_publique. Les règles de visibilité (produit actif,
boutique publique, vendeur validé, variante active...) restent dans Django,
protégées par un test de parité ; le rôle anitche_fastapi_ro n'a aucun droit
sur les tables du catalogue (ni stock exact, ni produits désactivés).

Construction du SQL :
- toute valeur envoyée par le client est un paramètre ($n), jamais du texte
  SQL. Seuls varient des fragments écrits dans ce module : un par mot
  cherché (8 au plus) et un par filtre présent ;
- le texte cherché est normalisé par catalogue_normaliser, la fonction des
  index, jamais en Python (règles différentes, index inutilisables) ;
- les jokers de LIKE (« \\ », « % », « _ ») sont échappés APRÈS la
  normalisation, dans le SQL : unaccent transforme « ％ », « ＿ » et « ＼ »
  (pleine chasse) en « % », « _ » et « \\ ».

Chaque requête commence par un commentaire « -- recherche:<nom> » : il
l'identifie dans les journaux de PostgreSQL (et dans le pool simulé des
tests).
"""
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import asyncpg
from fastapi import HTTPException

from app.core.cache import cache_get, cache_key, cached
from app.core.errors import SERVICE_UNAVAILABLE_MESSAGE

logger = logging.getLogger("anitche.fastapi.search")

# Pagination de Django (REST_FRAMEWORK : PageNumberPagination, 20 par page).
PAGE_SIZE = 20
# 50 pages = 1 000 résultats : borne le coût de l'OFFSET.
MAX_PAGE = 50
MAX_WORDS = 8
MIN_WORD_LENGTH = 2
MAX_PRICE = 9_999_999_999  # DecimalField(max_digits=12, decimal_places=2)
MAX_ID = 2**63 - 1  # bigint
SORTS = ("pertinence", "prix_asc", "prix_desc", "date_desc", "date_asc")
# Tranches de prix des facettes (FCFA) : min inclus, max exclu, la dernière
# sans maximum.
PRICE_BOUNDS = (0, 5_000, 10_000, 25_000, 50_000, 100_000)
CATEGORY_FACET_LIMIT = 20
SHOP_FACET_LIMIT = 10
SUGGESTION_CATEGORY_LIMIT = 2
SUGGESTION_SHOP_LIMIT = 2

DATABASE_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)


@dataclass(frozen=True)
class SearchFilters:
    """Filtres validés (app/routeurs/recherche.py). `text` : texte nettoyé
    (espaces fusionnés), None sans recherche."""

    text: str | None = None
    category: str | None = None
    shop: str | None = None
    min_price: int | None = None
    max_price: int | None = None

    @property
    def words(self) -> tuple[str, ...]:
        """Mots cherchés : 2 caractères au moins, les 8 premiers."""
        if not self.text:
            return ()
        return tuple(word for word in self.text.split(" ") if len(word) >= MIN_WORD_LENGTH)[:MAX_WORDS]


class _Query:
    """Paramètres d'une requête, numérotés au fur et à mesure : le texte SQL
    ne reçoit que des marqueurs $n."""

    def __init__(self):
        self.parameters: list[Any] = []

    def bind(self, value: Any) -> str:
        self.parameters.append(value)
        return f"${len(self.parameters)}"


def _as_id(value: str) -> int | None:
    """Identifiant si la valeur n'est faite que de chiffres ASCII et tient
    dans un bigint, sinon None."""
    if value.isascii() and value.isdigit():
        number = int(value)
        return number if number <= MAX_ID else None
    return None


def _escaped(placeholder: str) -> str:
    """Texte normalisé comme les index, puis jokers de LIKE échappés
    (« \\ » d'abord, caractère d'échappement par défaut de LIKE)."""
    return (
        f"replace(replace(replace(catalogue_normaliser({placeholder}::text), "
        r"'\', '\\'), '%', '\%'), '_', '\_')"
    )


@dataclass(frozen=True)
class _TextSearch:
    condition: str
    # Palier de pertinence (1 à 4), None s'il n'y a aucun mot utilisable.
    tier: str | None
    similarity: str


def _text_search(query: _Query, filters: SearchFilters) -> _TextSearch:
    """Un produit correspond si :
    1. chaque mot est contenu dans son nom ou sa description ;
    2. ou le texte ressemble à son nom (similarité de mots, pg_trgm : fautes
       de frappe, pluriels) ;
    3. ou chaque mot est contenu dans le nom de sa boutique, de sa catégorie
       ou de la catégorie parente.
    Paliers : 1 tous les mots dans le nom, 2 nom approchant, 3 nom et
    description, 4 boutique ou catégorie."""
    normalized = f"catalogue_normaliser({query.bind(filters.text)}::text)"
    patterns = [f"('%' || {_escaped(query.bind(word))} || '%')" for word in filters.words]
    similar_name = f"{normalized} <% p.nom_normalise"
    similarity = f"word_similarity({normalized}, p.nom_normalise)"
    if not patterns:
        return _TextSearch(condition=similar_name, tier=None, similarity=similarity)

    def all_words(column: str) -> str:
        return " AND ".join(f"{column} LIKE {pattern}" for pattern in patterns)

    # ANY(ARRAY(sous-requête)) : la sous-requête (vues des boutiques et des
    # catégories, petites) est calculée une fois, et les index boutique_id
    # et categorie_id restent utilisables avec les index trigrammes.
    shops = f"SELECT b.id FROM catalogue_boutique_publique AS b WHERE {all_words('b.nom_normalise')}"
    parents = f"SELECT cp.id FROM catalogue_categorie_publique AS cp WHERE {all_words('cp.nom_normalise')}"
    categories = (
        f"SELECT c.id FROM catalogue_categorie_publique AS c "
        f"WHERE ({all_words('c.nom_normalise')}) OR c.parent_id = ANY(ARRAY({parents}))"
    )
    condition = (
        f"(({all_words('p.texte_normalise')})"
        f" OR {similar_name}"
        f" OR p.boutique_id = ANY(ARRAY({shops}))"
        f" OR p.categorie_id = ANY(ARRAY({categories})))"
    )
    tier = (
        f"CASE WHEN {all_words('p.nom_normalise')} THEN 1"
        f" WHEN {similar_name} THEN 2"
        f" WHEN {all_words('p.texte_normalise')} THEN 3"
        f" ELSE 4 END"
    )
    return _TextSearch(condition=condition, tier=tier, similarity=similarity)


def _where(query: _Query, filters: SearchFilters) -> tuple[str, _TextSearch | None]:
    """Conditions de ProduitPublicListView (Django), plus la recherche
    tolérante aux accents et aux fautes."""
    conditions: list[str] = []
    search = None
    if filters.text:
        search = _text_search(query, filters)
        conditions.append(search.condition)

    if filters.category:
        # Slug ou id, sous-catégories d'un niveau comprises ; un slug peut
        # être numérique (« 2024 ») : les deux lectures sont acceptées.
        slug = query.bind(filters.category)
        options = [f"p.categorie_slug = {slug}", f"p.categorie_parent_slug = {slug}"]
        category_id = _as_id(filters.category)
        if category_id is not None:
            identifier = query.bind(category_id)
            options += [f"p.categorie_id = {identifier}", f"p.categorie_parent_id = {identifier}"]
        conditions.append("(" + " OR ".join(options) + ")")

    if filters.shop:
        # Id si la valeur n'est faite que de chiffres, sinon slug (Django).
        if filters.shop.isascii() and filters.shop.isdigit():
            shop_id = _as_id(filters.shop)
            conditions.append(f"p.boutique_id = {query.bind(shop_id)}" if shop_id is not None else "FALSE")
        else:
            conditions.append(f"p.boutique_slug = {query.bind(filters.shop)}")

    # Prix affiché : plus petit prix effectif des variantes actives (vue).
    if filters.min_price is not None:
        conditions.append(f"p.prix_min >= {query.bind(filters.min_price)}")
    if filters.max_price is not None:
        conditions.append(f"p.prix_min <= {query.bind(filters.max_price)}")

    return " AND ".join(conditions) or "TRUE", search


def _order_by(sort: str, search: _TextSearch | None) -> str:
    # Id en dernier critère : pagination stable à valeurs égales.
    if sort == "pertinence" and search is not None:
        keys = [f"{search.tier}"] if search.tier else []
        return ", ".join([*keys, f"{search.similarity} DESC", "p.date_creation DESC", "p.id DESC"])
    if sort == "prix_asc":
        return "p.prix_min ASC, p.date_creation DESC, p.id DESC"
    if sort == "prix_desc":
        return "p.prix_min DESC, p.date_creation DESC, p.id DESC"
    if sort == "date_asc":
        return "p.date_creation ASC, p.id ASC"
    return "p.date_creation DESC, p.id DESC"


PAGE_COLUMNS = (
    "p.id, p.nom, p.slug, p.prix_base, p.prix_min, p.image_principale, p.categorie_id, "
    "p.categorie_nom, p.boutique_id, p.boutique_nom, p.boutique_slug, p.en_stock, p.date_creation"
)


def page_query(filters: SearchFilters, sort: str, page: int) -> tuple[str, list[Any]]:
    query = _Query()
    where, search = _where(query, filters)
    order_by = _order_by(sort, search)
    offset = query.bind((page - 1) * PAGE_SIZE)
    sql = (
        "-- recherche:page\n"
        f"SELECT {PAGE_COLUMNS}\n"
        "FROM catalogue_produit_public AS p\n"
        f"WHERE {where}\n"
        f"ORDER BY {order_by}\n"
        f"LIMIT {PAGE_SIZE} OFFSET {offset}"
    )
    return sql, query.parameters


def count_query(filters: SearchFilters) -> tuple[str, list[Any]]:
    query = _Query()
    where, _ = _where(query, filters)
    sql = f"-- recherche:total\nSELECT count(*) FROM catalogue_produit_public AS p\nWHERE {where}"
    return sql, query.parameters


def summary_query(filters: SearchFilters) -> tuple[str, list[Any]]:
    """Total et facettes en une requête, sur tout l'ensemble filtré."""
    query = _Query()
    where, _ = _where(query, filters)
    thresholds = ", ".join(str(bound) for bound in PRICE_BOUNDS[1:])
    sql = f"""-- recherche:resume
WITH resultats AS MATERIALIZED (
    SELECT p.categorie_id, p.categorie_nom, p.categorie_slug, p.categorie_parent_id,
           p.boutique_id, p.boutique_nom, p.boutique_slug, p.prix_min
    FROM catalogue_produit_public AS p
    WHERE {where}
)
SELECT
    (SELECT count(*) FROM resultats) AS total,
    (SELECT min(prix_min) FROM resultats) AS prix_min,
    (SELECT max(prix_min) FROM resultats) AS prix_max,
    (SELECT coalesce(json_agg(json_build_object('tranche', t.tranche, 'nombre', t.nombre)
                              ORDER BY t.tranche), '[]')
     FROM (SELECT width_bucket(prix_min, ARRAY[{thresholds}]::numeric[]) AS tranche, count(*) AS nombre
           FROM resultats GROUP BY 1) AS t) AS tranches,
    (SELECT coalesce(json_agg(json_build_object('id', c.categorie_id, 'nom', c.categorie_nom,
                                                'slug', c.categorie_slug, 'parent', c.categorie_parent_id,
                                                'nombre', c.nombre)
                              ORDER BY c.nombre DESC, c.categorie_nom, c.categorie_id), '[]')
     FROM (SELECT categorie_id, categorie_nom, categorie_slug, categorie_parent_id, count(*) AS nombre
           FROM resultats WHERE categorie_id IS NOT NULL
           GROUP BY categorie_id, categorie_nom, categorie_slug, categorie_parent_id
           ORDER BY nombre DESC, categorie_nom, categorie_id
           LIMIT {CATEGORY_FACET_LIMIT}) AS c) AS categories,
    (SELECT coalesce(json_agg(json_build_object('id', b.boutique_id, 'nom', b.boutique_nom,
                                                'slug', b.boutique_slug, 'nombre', b.nombre)
                              ORDER BY b.nombre DESC, b.boutique_nom, b.boutique_id), '[]')
     FROM (SELECT boutique_id, boutique_nom, boutique_slug, count(*) AS nombre
           FROM resultats
           GROUP BY boutique_id, boutique_nom, boutique_slug
           ORDER BY nombre DESC, boutique_nom, boutique_id
           LIMIT {SHOP_FACET_LIMIT}) AS b) AS boutiques"""
    return sql, query.parameters


def suggestions_query(text: str, limit: int) -> tuple[str, list[Any]]:
    """Catégories actives (2 au plus), boutiques publiques (2 au plus) et
    produits visibles. Rang : 1 le nom commence par le texte, 2 un mot du nom
    commence par le texte, 3 le nom contient le texte, 4 nom approchant."""
    query = _Query()
    placeholder = query.bind(text)
    normalized = f"catalogue_normaliser({placeholder}::text)"
    escaped = _escaped(placeholder)
    prefix = f"({escaped} || '%')"
    word_prefix = f"('% ' || {escaped} || '%')"
    contains = f"('%' || {escaped} || '%')"
    limit_placeholder = query.bind(limit)

    def source(kind: str, view: str, alias: str, source_limit: str) -> str:
        name = f"{alias}.nom_normalise"
        rank = (
            f"CASE WHEN {name} LIKE {prefix} THEN 1 WHEN {name} LIKE {word_prefix} THEN 2"
            f" WHEN {name} LIKE {contains} THEN 3 ELSE 4 END"
        )
        return (
            f"(SELECT '{kind}' AS type, {alias}.id, {alias}.nom AS texte, {alias}.slug, {rank} AS rang,"
            f" word_similarity({normalized}, {name}) AS proximite"
            f" FROM {view} AS {alias}"
            f" WHERE {name} LIKE {contains} OR {normalized} <% {name}"
            f" ORDER BY rang, proximite DESC, texte, {alias}.id LIMIT {source_limit})"
        )

    sources = " UNION ALL ".join([
        source("categorie", "catalogue_categorie_publique", "c", str(SUGGESTION_CATEGORY_LIMIT)),
        source("boutique", "catalogue_boutique_publique", "b", str(SUGGESTION_SHOP_LIMIT)),
        source("produit", "catalogue_produit_public", "p", limit_placeholder),
    ])
    sql = (
        "-- recherche:suggestions\n"
        f"SELECT type, id, texte, slug FROM ({sources}) AS s\n"
        "ORDER BY rang, CASE type WHEN 'categorie' THEN 1 WHEN 'boutique' THEN 2 ELSE 3 END,"
        f" proximite DESC, texte, id\nLIMIT {limit_placeholder}"
    )
    return sql, query.parameters


# ------------------------------------------------------------ exécution


async def _run(method, sql: str, parameters: list[Any]):
    """Requête sur le pool en lecture seule. HTTPException 503 si PostgreSQL
    est indisponible, trop lent (statement_timeout) ou si un droit manque."""
    try:
        return await method(sql, *parameters)
    except DATABASE_ERRORS as exc:
        logger.error("Recherche impossible (PostgreSQL) : %s", type(exc).__name__)
        raise HTTPException(503, SERVICE_UNAVAILABLE_MESSAGE) from None


def _number(value) -> float | None:
    return None if value is None else float(value)


def _price_ranges(tranches: list[dict]) -> list[dict]:
    ranges = []
    for tranche in tranches:
        index = tranche["tranche"]
        upper = PRICE_BOUNDS[index + 1] if index + 1 < len(PRICE_BOUNDS) else None
        ranges.append({"min": PRICE_BOUNDS[index], "max": upper, "nombre": tranche["nombre"]})
    return ranges


def _json(value) -> list:
    # asyncpg renvoie le type json en texte (aucun codec configuré).
    return json.loads(value) if isinstance(value, str) else value


async def _compute_summary(db, filters: SearchFilters) -> dict:
    row = await _run(db.fetchrow, *summary_query(filters))
    return {
        "count": row["total"],
        "facettes": {
            "categories": _json(row["categories"]),
            "boutiques": _json(row["boutiques"]),
            "prix": {
                "min": _number(row["prix_min"]),
                "max": _number(row["prix_max"]),
                "tranches": _price_ranges(_json(row["tranches"])),
            },
        },
    }


async def fetch_summary(db, redis, filters: SearchFilters, ttl: int) -> dict:
    """{count, facettes}, en cache `ttl` secondes (page 1)."""
    key = cache_key("recherche:facettes", asdict(filters))
    return await cached(redis, key, ttl, lambda: _compute_summary(db, filters))


async def fetch_count(db, redis, filters: SearchFilters, ttl: int) -> int:
    """Total seul (pages suivantes), en cache `ttl` secondes ; repris des
    facettes en cache s'il y en a (même ensemble filtré)."""
    parameters = asdict(filters)

    async def compute() -> int:
        if ttl > 0:
            summary = await cache_get(redis, cache_key("recherche:facettes", parameters))
            if isinstance(summary, dict) and isinstance(summary.get("count"), int):
                return summary["count"]
        return await _run(db.fetchval, *count_query(filters))

    return await cached(redis, cache_key("recherche:total", parameters), ttl, compute)


async def fetch_page(db, filters: SearchFilters, sort: str, page: int) -> list:
    """Page de résultats : JAMAIS en cache (un produit masqué dans Django
    disparaît à la requête suivante)."""
    return await _run(db.fetch, *page_query(filters, sort, page))


async def fetch_suggestions(db, redis, text: str, limit: int, ttl: int) -> list[dict]:
    key = cache_key("recherche:suggestions", {"texte": text, "limite": limit})

    async def compute() -> list[dict]:
        rows = await _run(db.fetch, *suggestions_query(text, limit))
        return [{"type": row["type"], "texte": row["texte"], "id": row["id"], "slug": row["slug"]} for row in rows]

    return await cached(redis, key, ttl, compute)


# ------------------------------------------------------------ réponse


def media_url(media_base_url: str, path: str | None) -> str | None:
    """URL absolue d'une image, encodée comme Django (filepath_to_uri)."""
    if not path:
        return None
    return media_base_url + quote(path.replace("\\", "/").lstrip("/"), safe="/~!*()'")


def _iso(value: datetime) -> str:
    # Même forme que DRF (TIME_ZONE Africa/Abidjan = UTC) : « …Z ».
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decimal_string(value) -> str:
    # DecimalField(decimal_places=2) de DRF : « 4500.00 ».
    return f"{Decimal(str(value)):.2f}"


def product_result(row, media_base_url: str) -> dict:
    """Élément de liste, mêmes champs que ProduitPublicListSerializer.
    Jamais la quantité en stock : seulement `en_stock`."""
    prix_min = row["prix_min"] if row["prix_min"] is not None else row["prix_base"]
    return {
        "id": row["id"],
        "nom": row["nom"],
        "slug": row["slug"],
        "prix_base": _decimal_string(row["prix_base"]),
        "prix_min": float(prix_min),
        "image_principale": media_url(media_base_url, row["image_principale"]),
        "categorie": row["categorie_id"],
        "categorie_nom": row["categorie_nom"],
        "boutique": row["boutique_id"],
        "boutique_nom": row["boutique_nom"],
        "boutique_slug": row["boutique_slug"],
        "en_stock": bool(row["en_stock"]),
        "date_creation": _iso(row["date_creation"]),
    }
