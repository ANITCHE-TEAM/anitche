"""Intégration PostgreSQL + Redis : recherche sur les VRAIES vues.

Vues, fonction et index créés par la migration Django catalogue 0004 ;
droits posés par infra/postgres/fastapi_readonly.sql ; données insérées dans
les vraies tables Django par le compte administrateur (CatalogueSeed) et
lues par FastAPI avec le rôle en lecture seule, comme en production.
"""
import asyncio
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from fastapi.testclient import TestClient
from redis import Redis

from app.core import resources as resources_module
from app.services import search
from app.services.search import SearchFilters
from tests.fakes import make_settings

pytestmark = pytest.mark.integration

VIEWS = {"catalogue_produit_public", "catalogue_categorie_publique", "catalogue_boutique_publique"}
PRODUCT_VIEW_COLUMNS = {
    "id", "nom", "slug", "prix_base", "date_creation", "categorie_id", "categorie_nom", "categorie_slug",
    "categorie_parent_id", "categorie_parent_slug", "boutique_id", "boutique_nom", "boutique_slug",
    "prix_min", "en_stock", "image_principale", "nom_normalise", "texte_normalise",
}


def ids(response) -> list[int]:
    assert response.status_code == 200, response.text
    return [product["id"] for product in response.json()["results"]]


def search_all(client, **params) -> dict:
    response = client.get("/recherche/produits", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def _fetch(url: str, query: str, *args):
    connection = await asyncpg.connect(url)
    try:
        return await connection.fetch(query, *args)
    finally:
        await connection.close()


class CountingPool:
    """Pool réel, requêtes comptées (texte seulement)."""

    def __init__(self, pool):
        self.pool = pool
        self.queries: list[str] = []

    async def fetch(self, query, *args):
        self.queries.append(query)
        return await self.pool.fetch(query, *args)

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        return await self.pool.fetchrow(query, *args)

    async def fetchval(self, query, *args):
        self.queries.append(query)
        return await self.pool.fetchval(query, *args)

    def named(self, name: str) -> int:
        return sum(1 for query in self.queries if query.startswith(f"-- recherche:{name}\n"))


# ------------------------------------------------------------ visibilité


def test_product_hidden_in_django_is_hidden_in_search(catalogue, make_search_app):
    """Invisible dans Django = invisible dans la recherche : liste, total,
    facettes, suggestions et filtre par boutique. Un produit de référence
    visible, puis un produit par règle de masquage, tous nommés avec le même
    mot unique."""
    marker = catalogue.marker
    shop = catalogue.shop(f"Atelier {marker}")
    reference = catalogue.product(f"Lampe {marker} reference", shop=shop)
    hidden = {
        "produit désactivé": catalogue.product(f"Lampe {marker} desactivee", shop=shop, active=False),
        "sans variante": catalogue.product(f"Lampe {marker} sans variante", shop=shop, variants=()),
        "variantes inactives": catalogue.product(
            f"Lampe {marker} variantes inactives", shop=shop, variants=((9000, None, False, 5), (8000, None, False, 5)),
        ),
    }
    hidden_shops = {
        "boutique suspendue": catalogue.shop(f"Suspendue {marker}", suspended=True),
        "boutique fermée": catalogue.shop(f"Fermee {marker}", active=False),
        "vendeur KYC en attente": catalogue.shop(f"Attente {marker}", owner=catalogue.vendor(kyc="en_attente")),
        "vendeur KYC refusé": catalogue.shop(f"Refusee {marker}", owner=catalogue.vendor(kyc="refuse")),
        "vendeur inactif": catalogue.shop(f"Inactif {marker}", owner=catalogue.vendor(active=False)),
        "propriétaire client": catalogue.shop(f"Client {marker}", owner=catalogue.vendor(role="client")),
    }
    for rule, hidden_shop in hidden_shops.items():
        hidden[rule] = catalogue.product(f"Lampe {marker} {rule}", shop=hidden_shop)

    with TestClient(make_search_app()) as client:
        body = search_all(client, recherche=marker)
        assert [p["id"] for p in body["results"]] == [reference], "un produit masqué dans Django est visible"
        assert body["count"] == 1
        assert body["facettes"]["boutiques"] == [{"id": shop["id"], "nom": shop["nom"], "slug": shop["slug"], "nombre": 1}]
        assert sum(t["nombre"] for t in body["facettes"]["prix"]["tranches"]) == 1

        # Liste sans recherche, filtrée par boutique (slug et id).
        assert ids(client.get("/recherche/produits", params={"boutique": shop["slug"]})) == [reference]
        assert ids(client.get("/recherche/produits", params={"boutique": str(shop["id"])})) == [reference]
        for rule, hidden_shop in hidden_shops.items():
            for value in (hidden_shop["slug"], str(hidden_shop["id"])):
                response = client.get("/recherche/produits", params={"boutique": value})
                assert response.json()["count"] == 0 and ids(response) == [], rule

        # Suggestions : ni produit masqué, ni boutique non publique.
        suggestions = client.get("/recherche/suggestions", params={"recherche": marker}).json()["suggestions"]
        assert [(s["type"], s["id"]) for s in suggestions] == [("boutique", shop["id"]), ("produit", reference)]


def test_hiding_a_product_takes_effect_at_the_next_request(catalogue, make_search_app):
    """Aucun cache sur les résultats : boutique suspendue (compte admin) →
    produit absent dès la requête suivante. Le total, lui, vient du cache
    (jusqu'à 60 s de retard, décision 6)."""
    shop = catalogue.shop(f"Atelier {catalogue.marker}")
    product = catalogue.product(f"Vase {catalogue.marker}", shop=shop)

    with TestClient(make_search_app()) as client:
        assert ids(client.get("/recherche/produits", params={"recherche": catalogue.marker})) == [product]
        catalogue.execute("UPDATE vendeurs_boutique SET est_suspendue = true WHERE id = $1", shop["id"])
        body = search_all(client, recherche=catalogue.marker)
        assert body["results"] == []
        assert body["count"] == 1  # total en cache


# ------------------------------------------------------------ règles Django


def test_django_display_rules_are_reproduced(catalogue, make_search_app):
    """Prix affiché = plus petit prix effectif des variantes ACTIVES (promo
    comprise) ; `en_stock` sans quantité ; image principale choisie comme
    ProduitPublicListSerializer ; format des valeurs de la liste Django."""
    marker = catalogue.marker
    shop = catalogue.shop(f"Atelier {marker}")
    promo = catalogue.product(
        f"Promo {marker}", shop=shop, base_price=12000,
        variants=((12000, 8000, True, 2), (5000, None, False, 9)),  # variante inactive moins chère
        images=(("catalogue/produits/b.png", False, 0), ("catalogue/produits/a b.png", True, 5)),
        created=datetime(2026, 9, 1, 10, 0, 0, 123456, tzinfo=UTC),
    )
    no_stock = catalogue.product(
        f"Rupture {marker}", shop=shop, variants=((3000, None, True, 0),),
        images=(("catalogue/produits/deux.png", False, 2), ("catalogue/produits/un.png", False, 1)),
    )
    no_stock_row = catalogue.product(f"Sans ligne {marker}", shop=shop, variants=((4000, None, True, None),))
    inactive_only_in_stock = catalogue.product(
        f"Mixte {marker}", shop=shop, variants=((6000, None, True, 0), (7000, None, False, 50)),
        images=(("", True, 0),),
    )

    with TestClient(make_search_app(media_base_url="https://media.anitche.test/media/")) as client:
        results = {p["id"]: p for p in search_all(client, recherche=marker)["results"]}

    assert set(results) == {promo, no_stock, no_stock_row, inactive_only_in_stock}
    assert results[promo]["prix_min"] == 8000.0 and results[promo]["prix_base"] == "12000.00"
    assert results[promo]["en_stock"] is True
    assert results[promo]["image_principale"] == "https://media.anitche.test/media/catalogue/produits/a%20b.png"
    assert results[promo]["date_creation"] == "2026-09-01T10:00:00.123456Z"
    assert results[no_stock]["en_stock"] is False and results[no_stock]["prix_min"] == 3000.0
    assert results[no_stock]["image_principale"].endswith("/catalogue/produits/un.png")
    assert results[no_stock_row]["en_stock"] is False
    # Seule la variante INACTIVE a du stock : pas en stock ; image vide : null.
    assert results[inactive_only_in_stock]["en_stock"] is False
    assert results[inactive_only_in_stock]["prix_min"] == 6000.0
    assert results[inactive_only_in_stock]["image_principale"] is None
    for product in results.values():
        assert not {"quantite_disponible", "stock", "seuil_alerte", "description", "sku"} & set(product)


def test_category_filter_includes_subcategories(catalogue, make_search_app):
    """Filtre `categorie` de Django : slug ou id, sous-catégories d'un niveau
    comprises ; un produit d'une catégorie inactive reste visible (règle
    Django actuelle, dette MODULE_CATALOGUE.md § 9)."""
    marker = catalogue.marker
    shop = catalogue.shop(f"Atelier {marker}")
    parent = catalogue.category(f"Maison {marker}")
    child = catalogue.category(f"Cuisine {marker}", parent=parent)
    other = catalogue.category(f"Mode {marker}")
    inactive = catalogue.category(f"Ancienne {marker}", active=False)
    # Noms sans le mot unique : la recherche « maison <mot> » ne doit les
    # trouver que par le nom de leur catégorie.
    in_parent = catalogue.product("Nappe", shop=shop, category=parent)
    in_child = catalogue.product("Marmite", shop=shop, category=child)
    in_other = catalogue.product("Chemise", shop=shop, category=other)
    in_inactive = catalogue.product("Pilon", shop=shop, category=inactive)

    with TestClient(make_search_app()) as client:
        def by_category(value):
            return set(ids(client.get("/recherche/produits", params={"categorie": value})))

        assert by_category(parent["slug"]) == {in_parent, in_child}
        assert by_category(str(parent["id"])) == {in_parent, in_child}
        assert by_category(child["slug"]) == {in_child}
        assert by_category(other["slug"]) == {in_other}
        assert by_category(inactive["slug"]) == {in_inactive}
        assert in_inactive in set(ids(client.get("/recherche/produits", params={"recherche": marker})))

        # Recherche par nom de catégorie : la catégorie parente compte.
        assert set(ids(client.get("/recherche/produits", params={"recherche": f"maison {marker}"}))) == {in_parent, in_child}
        # Catégorie inactive : jamais suggérée ; catégorie active : oui.
        suggestions = client.get("/recherche/suggestions", params={"recherche": f"ancienne {marker}"}).json()
        assert inactive["id"] not in [s["id"] for s in suggestions["suggestions"] if s["type"] == "categorie"]
        suggestions = client.get("/recherche/suggestions", params={"recherche": f"cuisine {marker}"}).json()
        assert ("categorie", child["id"]) in [(s["type"], s["id"]) for s in suggestions["suggestions"]]


# ------------------------------------------------------------ recherche


def test_accents_case_and_typos(catalogue, make_search_app):
    """Sans accents ni majuscules (catalogue_normaliser) et tolérant aux
    fautes de frappe (similarité de mots pg_trgm, seuil 0,6)."""
    shop = catalogue.shop(f"Atelier {catalogue.marker}")
    robe = catalogue.product("Robe Baoulé traditionnelle", shop=shop)
    ecouteurs = catalogue.product("Écouteurs Bluetooth", shop=shop)
    chemise = catalogue.product("Chemise en wax", shop=shop)
    beurre = catalogue.product("Beurre de karité pur", shop=shop)

    expectations = {
        "baoule": robe,
        "BAOULÉ": robe,
        "Baoulé": robe,
        "baoulle": robe,  # faute de frappe
        "ecouteurs": ecouteurs,
        "ÉCOUTEUR": ecouteurs,
        "chemize": chemise,  # faute de frappe
        "wax chemise": chemise,  # mots dans le désordre
        "karitee": beurre,  # faute de frappe
        "beure de karite": beurre,
    }
    with TestClient(make_search_app()) as client:
        for text, expected in expectations.items():
            found = ids(client.get("/recherche/produits", params={"recherche": text}))
            assert expected in found, text
            assert found[0] == expected, (text, found)
        suggestions = client.get("/recherche/suggestions", params={"recherche": "baou"}).json()["suggestions"]
        assert ("produit", robe) in [(s["type"], s["id"]) for s in suggestions]
        suggestions = client.get("/recherche/suggestions", params={"recherche": "ecout"}).json()["suggestions"]
        assert ("produit", ecouteurs) in [(s["type"], s["id"]) for s in suggestions]


def test_relevance_tiers(catalogue, make_search_app):
    """Palier 1 nom, 3 description, 4 boutique : dans cet ordre."""
    marker = catalogue.marker
    shop = catalogue.shop(f"Maison {marker}")
    in_shop_name = catalogue.product("Mortier", shop=shop)
    in_description = catalogue.product("Savon noir", shop=shop, description=f"Parfum {marker} naturel")
    in_name = catalogue.product(f"Beurre {marker}", shop=shop)

    with TestClient(make_search_app()) as client:
        assert ids(client.get("/recherche/produits", params={"recherche": marker})) == [in_name, in_description, in_shop_name]


def test_facets_are_consistent_with_filtered_results(catalogue, make_search_app):
    """Facettes calculées sur tout l'ensemble filtré (recherche et filtres) :
    mêmes totaux que les résultats, par catégorie, boutique et tranche."""
    marker = catalogue.marker
    first_shop, second_shop = catalogue.shop(f"Premier {marker}"), catalogue.shop(f"Second {marker}")
    root = catalogue.category(f"Racine {marker}")
    child = catalogue.category(f"Enfant {marker}", parent=root)
    other = catalogue.category(f"Autre {marker}")
    prices = [(3000, first_shop, root), (7000, first_shop, child), (12000, second_shop, child),
              (30000, second_shop, other), (150000, first_shop, None), (26000, first_shop, other)]
    for price, shop, category in prices:
        catalogue.product(f"Objet {marker} {price}", shop=shop, category=category, variants=((price, None, True, 1),))
    catalogue.product(f"Objet {marker} masque", shop=catalogue.shop(f"Suspendue {marker}", suspended=True))

    with TestClient(make_search_app()) as client:
        for params in (
            {"recherche": marker},
            {"recherche": marker, "prix_min": 5000},
            {"recherche": marker, "categorie": root["slug"]},
            {"recherche": marker, "prix_max": 29999, "boutique": first_shop["slug"]},
        ):
            body = search_all(client, **params)
            results = body["results"]
            facets = body["facettes"]
            assert body["count"] == len(results) > 0, params

            expected_categories = {}
            for product in results:
                if product["categorie"] is not None:
                    expected_categories[product["categorie"]] = expected_categories.get(product["categorie"], 0) + 1
            assert {c["id"]: c["nombre"] for c in facets["categories"]} == expected_categories, params
            expected_shops = {}
            for product in results:
                expected_shops[product["boutique"]] = expected_shops.get(product["boutique"], 0) + 1
            assert {b["id"]: b["nombre"] for b in facets["boutiques"]} == expected_shops, params

            listed = [product["prix_min"] for product in results]
            assert facets["prix"]["min"] == min(listed) and facets["prix"]["max"] == max(listed)
            assert sum(t["nombre"] for t in facets["prix"]["tranches"]) == len(results)
            for tranche in facets["prix"]["tranches"]:
                inside = [p for p in listed if p >= tranche["min"] and (tranche["max"] is None or p < tranche["max"])]
                assert len(inside) == tranche["nombre"], (params, tranche)
        child_facet = next(c for c in search_all(client, recherche=marker)["facettes"]["categories"] if c["id"] == child["id"])
        assert child_facet == {"id": child["id"], "nom": child["nom"], "slug": child["slug"], "parent": root["id"], "nombre": 2}


def test_like_wildcards_are_escaped(catalogue, make_search_app):
    """« % », « _ » et « \\ » ne sont jamais des jokers, y compris en pleine
    chasse (« ％ », « ＿ », « ＼ »), que unaccent transforme en jokers :
    échappement APRÈS normalisation. Sans échappement, « __ » ou « %% »
    renverraient tout le catalogue."""
    marker = catalogue.marker
    shop = catalogue.shop(f"Atelier {marker}")
    coton = catalogue.product(f"Coton 100% bio {marker}", shop=shop)
    tissu = catalogue.product(f"Tissu_{marker}", shop=shop)
    catalogue.product(f"Pagne {marker}", shop=shop)

    with TestClient(make_search_app()) as client:
        assert search_all(client)["count"] >= 3
        for text in ("%%", "__", "％％", "＿＿", "\\\\", "＼＼", "%_", "% _ \\"):
            body = search_all(client, recherche=text)
            assert body["count"] == 0 and body["results"] == [], text
        for text in ("%%%", "___", "％％％", "＿＿＿"):
            response = client.get("/recherche/suggestions", params={"recherche": text})
            assert response.json()["suggestions"] == [], text
        # Le caractère lui-même reste cherchable, littéralement.
        assert ids(client.get("/recherche/produits", params={"recherche": "100%"})) == [coton]
        assert ids(client.get("/recherche/produits", params={"recherche": "100％"})) == [coton]
        assert ids(client.get("/recherche/produits", params={"recherche": f"tissu_{marker}"}))[0] == tissu


# ------------------------------------------------------------ tri, pages


def test_sorting_and_pagination(catalogue, make_search_app):
    marker = catalogue.marker
    shop = catalogue.shop(f"Atelier {marker}")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    created = [
        catalogue.product(f"Article {marker} {n}", shop=shop, variants=(((n % 7 + 1) * 1000, None, True, 1),),
                          created=start + timedelta(hours=n))
        for n in range(23)
    ]
    newest_first = list(reversed(created))

    with TestClient(make_search_app(public_base_url="https://anitche.test/fast")) as client:
        first = search_all(client, boutique=shop["slug"])
        assert first["count"] == 23 and [p["id"] for p in first["results"]] == newest_first[:20]
        assert first["next"] == f"https://anitche.test/fast/recherche/produits?boutique={shop['slug']}&page=2"
        second = search_all(client, boutique=shop["slug"], page=2)
        assert [p["id"] for p in second["results"]] == newest_first[20:]
        assert second["next"] is None and second["facettes"] is None
        assert second["previous"] == f"https://anitche.test/fast/recherche/produits?boutique={shop['slug']}"
        response = client.get("/recherche/produits", params={"boutique": shop["slug"], "page": 3})
        assert response.status_code == 404 and response.json()["errors"] == {"code": ["page_invalide"]}

        ascending = search_all(client, boutique=shop["slug"], tri="date_asc")
        assert [p["id"] for p in ascending["results"]] == created[:20]
        for sort, reverse in (("prix_asc", False), ("prix_desc", True)):
            products = search_all(client, boutique=shop["slug"], tri=sort)["results"]
            prices = [p["prix_min"] for p in products]
            assert prices == sorted(prices, reverse=reverse), sort
            # À prix égal : plus récent d'abord (puis id).
            for before, after in zip(products, products[1:]):
                if before["prix_min"] == after["prix_min"]:
                    assert before["date_creation"] > after["date_creation"]


def test_invalid_parameters_are_400_on_the_real_application(make_search_app):
    with TestClient(make_search_app()) as client:
        for path in (
            "/recherche/produits?tri=note",
            "/recherche/produits?prix_min=1.5",
            "/recherche/produits?recherche=a",
            "/recherche/produits?categorie=a%20b",
            "/recherche/suggestions?recherche=ab",
        ):
            response = client.get(path)
            assert response.status_code == 400 and response.json()["success"] is False, path


# ------------------------------------------------------------ cache


def test_second_call_runs_no_sql_for_facets(catalogue, make_search_app, clean_redis):
    """Cache Redis réel : 2e appel identique sans requête SQL pour le total
    et les facettes ; page relue ; clé avec TTL de 60 s au plus."""
    shop = catalogue.shop(f"Atelier {catalogue.marker}")
    catalogue.product(f"Panier {catalogue.marker}", shop=shop)

    with TestClient(make_search_app()) as client:
        pool = CountingPool(client.app.state.db)
        client.app.state.db = pool
        first = search_all(client, recherche=catalogue.marker)
        second = search_all(client, recherche=catalogue.marker)
        client.get("/recherche/suggestions", params={"recherche": catalogue.marker})
        client.get("/recherche/suggestions", params={"recherche": catalogue.marker})
        client.app.state.db = pool.pool

    assert first == second
    assert pool.named("resume") == 1 and pool.named("page") == 2 and pool.named("suggestions") == 1
    with Redis.from_url(clean_redis, decode_responses=True) as redis:
        keys = redis.keys("fastapi:cache:*")
        assert len(keys) == 2 and all(0 < redis.ttl(key) <= 60 for key in keys)
        assert not any(catalogue.marker in key for key in keys)


# ------------------------------------------------------------ droits


def test_role_reads_the_three_views_and_no_catalogue_table(admin_database_url, ro_database_url):
    """Moindre privilège : le rôle lit les trois vues, et AUCUNE table du
    catalogue ni des boutiques (ni stock exact, ni statut KYC)."""
    tables = asyncio.run(_fetch(
        admin_database_url,
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
          AND (table_name LIKE 'catalogue\\_%' OR table_name LIKE 'vendeurs\\_%')
        ORDER BY table_name
        """,
    ))
    names = [row["table_name"] for row in tables]
    assert {"catalogue_produit", "catalogue_stock", "catalogue_varianteproduit", "vendeurs_boutique"} <= set(names)

    async def check():
        connection = await asyncpg.connect(ro_database_url)
        try:
            for name in names:
                # Nom lu dans information_schema (jamais une donnée externe).
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await connection.fetch(f'SELECT 1 FROM "{name}" LIMIT 1')
            for query in (
                "SELECT quantite_disponible FROM catalogue_stock",
                "SELECT statut_kyc FROM utilisateurs_utilisateur",
                "SELECT description FROM catalogue_produit",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await connection.fetch(query)
            for view in sorted(VIEWS):
                await connection.fetch(f"SELECT * FROM {view} LIMIT 1")
        finally:
            await connection.close()

    asyncio.run(check())


def test_product_view_exposes_only_public_columns(admin_database_url):
    rows = asyncio.run(_fetch(
        admin_database_url,
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'catalogue_produit_public'",
    ))
    assert {row["column_name"] for row in rows} == PRODUCT_VIEW_COLUMNS


# ------------------------------------------------------------ plans


async def _explain_execute(connection, name: str, parameters: list) -> str:
    # EXPLAIN EXECUTE n'accepte pas de paramètres liés : valeurs citées par
    # PostgreSQL lui-même (format %L).
    template = f"EXPLAIN EXECUTE {name}(" + ", ".join(["%L"] * len(parameters)) + ")"
    arguments = ", ".join(f"${index + 1}::text" for index in range(len(parameters)))
    statement = await connection.fetchval(f"SELECT format('{template}', {arguments})", *[str(p) for p in parameters])
    return "\n".join(row[0] for row in await connection.fetch(statement))


# Réglages qui obligent le planificateur à montrer les index utilisables (la
# base de test est trop petite pour qu'il les choisisse seul) : recherche
# texte, parcours bitmap seulement ; tri par date, sans tri explicite (seul
# l'index de date donne l'ordre).
BITMAP_ONLY = ("enable_seqscan", "enable_indexscan", "enable_nestloop", "enable_mergejoin", "jit")
ORDERED_BY_INDEX = ("enable_seqscan", "enable_sort", "jit")


def test_generated_queries_can_use_the_indexes(ro_database_url):
    """Les expressions de la vue correspondent aux index de la migration,
    avec des paramètres liés : plans générique ET personnalisé (asyncpg
    prépare les requêtes)."""
    cases = {
        "total": (search.count_query(SearchFilters(text="chemise wax")), BITMAP_ONLY,
                  {"catalogue_produit_texte_trgm", "catalogue_produit_nom_trgm"}),
        "page": (search.page_query(SearchFilters(text="chemise wax"), "pertinence", 1), BITMAP_ONLY,
                 {"catalogue_produit_texte_trgm", "catalogue_produit_nom_trgm"}),
        "suggestions": (search.suggestions_query("chem", 8), BITMAP_ONLY, {"catalogue_produit_nom_trgm"}),
        "date": (search.page_query(SearchFilters(), "date_desc", 1), ORDERED_BY_INDEX, {"catalogue_produit_date_id"}),
    }

    async def check():
        connection = await asyncpg.connect(ro_database_url)
        try:
            for number, (name, ((sql, parameters), settings, indexes)) in enumerate(cases.items()):
                for mode in ("force_generic_plan", "force_custom_plan"):
                    async with connection.transaction(readonly=True):
                        for setting in settings:
                            await connection.execute(f"SET LOCAL {setting} = off")
                        await connection.execute(f"SET LOCAL plan_cache_mode = {mode}")
                        await connection.execute(f"PREPARE recherche_{number} AS {sql}")
                        plan = await _explain_execute(connection, f"recherche_{number}", parameters)
                        await connection.execute(f"DEALLOCATE recherche_{number}")
                    missing = {index for index in indexes if index not in plan}
                    assert not missing, (name, mode, missing, plan)
        finally:
            await connection.close()

    asyncio.run(check())


def test_application_pool_forces_custom_plans(ro_database_url, redis_url):
    async def check():
        opened = await resources_module.open_resources(make_settings(database_url=ro_database_url, redis_url=redis_url))
        try:
            return await opened.db.fetchval("SHOW plan_cache_mode"), await opened.db.fetchval("SHOW transaction_read_only")
        finally:
            await resources_module.close_resources(opened)

    assert asyncio.run(check()) == ("force_custom_plan", "on")
