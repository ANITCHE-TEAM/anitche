"""Module 2 : recherche (app/routeurs/recherche.py, app/services/search.py).

Pool PostgreSQL simulé (tests/fakes.py) : on vérifie ici les paramètres,
le SQL construit (jamais de valeur du client dans le texte, vues seules),
la réponse, le cache et les pannes. Le comportement du SQL sur les vraies
vues (accents, fautes, visibilité, échappement réel, droits) est testé dans
tests/integration/test_recherche_vues.py.

Diagnostic du rapport module 2 couvert : 1-2 (maquette et état global),
3-4 et 10 (paramètres), 5 (facettes), 6 (tri par note), 11-12 (champs et
suggestions), A-C (description, tri par défaut, profondeur de page).
"""
import logging
import re
from types import MappingProxyType

import asyncpg
import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.resources import DATABASE_SERVER_SETTINGS, Resources
from app.main import create_app
from app.services import ia_service, search
from app.services.search import SearchFilters
from tests.fakes import FakeDjango, FakePool, make_settings, product_row

VIEWS = {"catalogue_produit_public", "catalogue_categorie_publique", "catalogue_boutique_publique"}
DJANGO_LIST_FIELDS = {
    "id", "nom", "slug", "prix_base", "prix_min", "image_principale", "categorie", "categorie_nom",
    "boutique", "boutique_nom", "boutique_slug", "en_stock", "date_creation",
}
ERROR_KEYS = {"success", "status_code", "detail", "errors"}


def errors_of(response) -> dict:
    assert response.status_code == 400, response.text
    body = response.json()
    assert set(body) == ERROR_KEYS and body["success"] is False and body["status_code"] == 400
    return body["errors"]


def placeholders(sql: str) -> set[int]:
    return {int(number) for number in re.findall(r"\$(\d+)", sql)}


def relations(sql: str) -> set[str]:
    return set(re.findall(r"\b(?:FROM|JOIN)\s+(\w+)", sql))


# ------------------------------------------------------------ paramètres


@pytest.mark.parametrize(
    "query, field, message",
    [
        ("recherche=a", "recherche", "Assurez-vous que ce champ comporte au moins 2\xa0caractères."),
        ("recherche=%20a%20%20", "recherche", "Assurez-vous que ce champ comporte au moins 2\xa0caractères."),
        ("recherche=" + "x" * 101, "recherche", "Assurez-vous que ce champ comporte au plus 100\xa0caractères."),
        ("recherche=ab%00cd", "recherche", "Ce champ contient des caractères non autorisés."),
        ("recherche=ab%07cd", "recherche", "Ce champ contient des caractères non autorisés."),
        ("categorie=" + "c" * 121, "categorie", "Assurez-vous que ce champ comporte au plus 120\xa0caractères."),
        ("categorie=mode;drop", "categorie", "Ce champ ne doit contenir que des lettres, des nombres, des tirets bas _ et des traits d'union."),
        ("categorie=mod%C3%A9", "categorie", "Ce champ ne doit contenir que des lettres, des nombres, des tirets bas _ et des traits d'union."),
        ("boutique=" + "b" * 141, "boutique", "Assurez-vous que ce champ comporte au plus 140\xa0caractères."),
        ("boutique=karite%20dore", "boutique", "Ce champ ne doit contenir que des lettres, des nombres, des tirets bas _ et des traits d'union."),
        ("prix_min=1500.75", "prix_min", "Un nombre entier valide est requis."),
        ("prix_min=abc", "prix_min", "Un nombre entier valide est requis."),
        ("prix_min=inf", "prix_min", "Un nombre entier valide est requis."),
        ("prix_max=nan", "prix_max", "Un nombre entier valide est requis."),
        ("prix_max=1e308", "prix_max", "Un nombre entier valide est requis."),
        ("prix_min=-1", "prix_min", "Assurez-vous que cette valeur est supérieure ou égale à\xa00."),
        ("prix_max=10000000000", "prix_max", "Assurez-vous que cette valeur est inférieure ou égale à 9999999999."),
        ("prix_min=50000&prix_max=10", "prix_max", "Assurez-vous que prix_max est supérieur ou égal à prix_min."),
        ("tri=note", "tri", "«\xa0note\xa0» n'est pas un choix valide."),
        ("tri=nimporte", "tri", "«\xa0nimporte\xa0» n'est pas un choix valide."),
        ("page=0", "page", "Assurez-vous que cette valeur est supérieure ou égale à\xa01."),
        ("page=51", "page", "Assurez-vous que cette valeur est inférieure ou égale à 50."),
        ("page=abc", "page", "Un nombre entier valide est requis."),
    ],
)
def test_invalid_product_parameters_are_400_with_the_field_key(client, db, query, field, message):
    """Diagnostic 3, 4, 10 : refus explicites (Django ignore en silence),
    format commun, clé du paramètre ; aucune requête SQL."""
    errors = errors_of(client.get(f"/recherche/produits?{query}"))
    assert errors == {field: [message]}
    assert db.search_calls("page") == [] and db.search_calls("resume") == []


@pytest.mark.parametrize(
    "query, field, message",
    [
        ("", "recherche", "Ce champ est obligatoire."),
        ("recherche=", "recherche", "Assurez-vous que ce champ comporte au moins 3\xa0caractères."),
        ("recherche=ab", "recherche", "Assurez-vous que ce champ comporte au moins 3\xa0caractères."),
        ("recherche=%20%20ab%20%20", "recherche", "Assurez-vous que ce champ comporte au moins 3\xa0caractères."),
        ("recherche=" + "x" * 51, "recherche", "Assurez-vous que ce champ comporte au plus 50\xa0caractères."),
        ("recherche=abc%00", "recherche", "Ce champ contient des caractères non autorisés."),
        ("recherche=wax&limite=0", "limite", "Assurez-vous que cette valeur est supérieure ou égale à\xa01."),
        ("recherche=wax&limite=11", "limite", "Assurez-vous que cette valeur est inférieure ou égale à 10."),
    ],
)
def test_invalid_suggestion_parameters_are_400(client, db, query, field, message):
    """Décision 5 : 3 caractères au moins (2 : index trigramme sans effet)."""
    assert errors_of(client.get(f"/recherche/suggestions?{query}")) == {field: [message]}
    assert db.calls == []


def test_old_parameters_are_ignored(client, db):
    """Ancien contrat (q, boutique_id, par_page) : ignoré, comme tout
    paramètre inconnu. Aucun consommateur à ce jour."""
    response = client.get("/recherche/produits?q=karite&boutique_id=3&par_page=50")
    assert response.status_code == 200
    ((sql, params),) = db.search_calls("page")
    assert params == (0,) and "catalogue_normaliser" not in sql


def test_empty_search_text_means_no_search(client, db):
    """Comme Django : `recherche=` (champ vide) liste le catalogue."""
    assert client.get("/recherche/produits?recherche=%20%20").status_code == 200
    ((sql, params),) = db.search_calls("page")
    assert "catalogue_normaliser" not in sql and params == (0,)


def test_search_text_is_cleaned_and_limited_to_eight_words(client, db):
    """Espaces fusionnés, mots d'un caractère ignorés, 8 mots au plus."""
    text = "  un   a  deux trois quatre cinq six sept huit neuf dix "
    assert client.get("/recherche/produits", params={"recherche": text}).status_code == 200
    ((sql, params),) = db.search_calls("page")
    cleaned = "un a deux trois quatre cinq six sept huit neuf dix"
    assert params[0] == cleaned
    assert list(params[1:-1]) == ["un", "deux", "trois", "quatre", "cinq", "six", "sept", "huit"]


def test_decomposed_accents_are_composed_before_sql(client, db):
    """« é » saisi en deux caractères (e + accent) : composé (NFC) pour que
    unaccent le reconnaisse ; minuscules et accents : SQL seulement."""
    client.get("/recherche/produits", params={"recherche": "Baoulé"})
    ((_, params),) = db.search_calls("page")
    assert params[0] == "Baoulé" and len(params[0]) == 6


# ------------------------------------------------------------ SQL


MALICIOUS = [
    "zz'; DROP TABLE catalogue_produit; --",
    "zz\" OR 1=1 --",
    "zz $1 $2 ::text",
    "zz %% __ \\\\",
    "zz ％ ＿ ＼",
]


@pytest.mark.parametrize("text", MALICIOUS)
def test_client_values_are_parameters_never_sql_text(client, db, text):
    """Requêtes paramétrées : le texte SQL est IDENTIQUE pour une valeur
    malveillante et pour une valeur anodine de même forme (même nombre de
    mots, mêmes filtres) ; les valeurs ne sont que dans les paramètres, et
    les marqueurs $n correspondent exactement à ces paramètres."""
    benign = " ".join("x" * len(word) for word in text.split())

    def run(value: str, category: str, shop: str) -> list[tuple[str, tuple]]:
        db.calls.clear()
        params = {"recherche": value, "categorie": category, "boutique": shop, "prix_min": 7, "prix_max": 9}
        assert client.get("/recherche/produits", params=params).status_code == 200
        assert client.get("/recherche/suggestions", params={"recherche": value}).status_code == 200
        return list(db.calls)

    malicious_calls = run(text, "zz-cat", "zz-shop")
    benign_calls = run(benign, "yy-cat", "yy-shop")
    assert len(malicious_calls) == 3  # résumé, page, suggestions (cache vide : autres clés)
    for (sql, args), (benign_sql, _) in zip(malicious_calls, benign_calls):
        assert sql == benign_sql
        assert text not in sql
        assert placeholders(sql) == set(range(1, len(args) + 1))
    assert text in malicious_calls[1][1]  # la valeur est bien transmise, en paramètre


def test_queries_read_only_the_three_public_views(client, db):
    """Aucune table du catalogue : les règles de visibilité restent dans
    Django (vues de la migration catalogue 0004)."""
    client.get("/recherche/produits?recherche=karite%20dore&categorie=12&boutique=14&prix_min=1&prix_max=9")
    client.get("/recherche/produits?recherche=karite&page=2")
    client.get("/recherche/suggestions?recherche=kar")
    assert {name for name, _ in [(q.split("\n", 1)[0], None) for q, _ in db.calls]} == {
        "-- recherche:resume", "-- recherche:page", "-- recherche:total", "-- recherche:suggestions",
    }
    for sql, _ in db.calls:
        assert relations(sql) - {"resultats"} <= VIEWS, relations(sql)


def test_like_wildcards_are_escaped_after_normalisation():
    """Jokers échappés APRÈS catalogue_normaliser, dans le SQL : unaccent
    transforme « ％ » (pleine chasse) en « % », un échappement en Python
    avant la normalisation laisserait passer ce joker. Effet réel vérifié
    sur PostgreSQL (tests d'intégration)."""
    sql, params = search.page_query(SearchFilters(text="100% coton"), "pertinence", 1)
    escaped = (
        "replace(replace(replace(catalogue_normaliser($2::text), "
        r"'\', '\\'), '%', '\%'), '_', '\_')"
    )
    assert f"LIKE ('%' || {escaped} || '%')" in sql
    assert params[1] == "100%"  # tel quel : échappé par PostgreSQL
    suggestions_sql, _ = search.suggestions_query("100%", 8)
    assert escaped.replace("$2", "$1") in suggestions_sql


def test_text_search_matches_name_description_shop_and_category():
    """Décision 2 : aussi dans les noms de boutique et de catégorie (et la
    catégorie parente) ; pertinence par paliers, puis similarité au nom."""
    sql, _ = search.page_query(SearchFilters(text="karite pur"), "pertinence", 1)
    assert "p.texte_normalise LIKE" in sql
    assert "catalogue_normaliser($1::text) <% p.nom_normalise" in sql
    assert "p.boutique_id = ANY(ARRAY(SELECT b.id FROM catalogue_boutique_publique AS b" in sql
    assert "p.categorie_id = ANY(ARRAY(SELECT c.id FROM catalogue_categorie_publique AS c" in sql
    assert "c.parent_id = ANY(ARRAY(SELECT cp.id FROM catalogue_categorie_publique AS cp" in sql
    order_by = sql.split("ORDER BY", 1)[1]
    assert order_by.lstrip().startswith("CASE WHEN p.nom_normalise LIKE")
    assert "word_similarity(catalogue_normaliser($1::text), p.nom_normalise) DESC, p.date_creation DESC, p.id DESC" in order_by


def test_text_without_usable_word_only_uses_name_similarity():
    """« a b » : aucun mot de 2 caractères ; ni LIKE ni palier (un entier
    seul dans ORDER BY désignerait une colonne)."""
    sql, params = search.page_query(SearchFilters(text="a b"), "pertinence", 1)
    assert "LIKE" not in sql and "CASE" not in sql
    assert params == ["a b", 0]


@pytest.mark.parametrize(
    "shop, condition, value",
    [("14", "p.boutique_id = $1", 14), ("karite-dore", "p.boutique_slug = $1", "karite-dore"), ("0014", "p.boutique_id = $1", 14)],
)
def test_shop_filter_is_id_for_digits_and_slug_otherwise(shop, condition, value):
    sql, params = search.page_query(SearchFilters(shop=shop), "date_desc", 1)
    assert condition in sql and params[0] == value


def test_shop_id_beyond_bigint_matches_nothing():
    sql, params = search.page_query(SearchFilters(shop="9" * 30), "date_desc", 1)
    assert "WHERE FALSE" in sql and params == [0]


def test_category_filter_includes_subcategories_and_numeric_slugs():
    """Comme Django : slug ou id, catégorie parente comprise ; « 2024 » est
    lu comme slug ET comme id."""
    sql, params = search.page_query(SearchFilters(category="2024"), "date_desc", 1)
    assert "(p.categorie_slug = $1 OR p.categorie_parent_slug = $1 OR p.categorie_id = $2 OR p.categorie_parent_id = $2)" in sql
    assert params == ["2024", 2024, 0]
    sql, params = search.page_query(SearchFilters(category="mode"), "date_desc", 1)
    assert "(p.categorie_slug = $1 OR p.categorie_parent_slug = $1)" in sql and params == ["mode", 0]


@pytest.mark.parametrize(
    "query, order_by",
    [
        ("", "p.date_creation DESC, p.id DESC"),
        ("tri=pertinence", "p.date_creation DESC, p.id DESC"),
        ("tri=date_desc", "p.date_creation DESC, p.id DESC"),
        ("tri=date_asc", "p.date_creation ASC, p.id ASC"),
        ("tri=prix_asc", "p.prix_min ASC, p.date_creation DESC, p.id DESC"),
        ("tri=prix_desc", "p.prix_min DESC, p.date_creation DESC, p.id DESC"),
        ("recherche=wax&tri=prix_asc", "p.prix_min ASC, p.date_creation DESC, p.id DESC"),
    ],
)
def test_sort_orders_end_with_id_for_stable_pages(client, db, query, order_by):
    """Diagnostic B et 6 : plus récents par défaut sans recherche, plus de tri
    par note ; id en dernier (Django s'arrête à la date)."""
    assert client.get(f"/recherche/produits?{query}").status_code == 200
    ((sql, _),) = db.search_calls("page")
    assert sql.split("ORDER BY ", 1)[1].startswith(order_by + "\nLIMIT 20 OFFSET $")


def test_default_sort_with_search_is_relevance(client, db):
    client.get("/recherche/produits?recherche=wax")
    ((sql, _),) = db.search_calls("page")
    assert "ORDER BY CASE WHEN" in sql


def test_pagination_is_twenty_per_page_with_offset_parameter(client, db):
    db.search_count = 45
    client.get("/recherche/produits?page=3")
    ((sql, params),) = db.search_calls("page")
    assert sql.endswith("LIMIT 20 OFFSET $1") and params == (40,)


# ------------------------------------------------------------ réponse


def test_response_has_the_django_envelope_and_fields_only(client, db):
    """Diagnostic 11 et A : champs de la liste Django, jamais de quantité en
    stock ni de description ; valeurs au format de Django."""
    db.search_rows = [product_row()]
    body = client.get("/recherche/produits").json()
    assert set(body) == {"count", "next", "previous", "results", "facettes"}
    (item,) = body["results"]
    assert set(item) == DJANGO_LIST_FIELDS
    assert item == {
        "id": 41,
        "nom": "Beurre de karité pur",
        "slug": "beurre-de-karite-pur-3d85cf",
        "prix_base": "3500.00",
        "prix_min": 3500.0,
        "image_principale": "http://localhost:8000/media/catalogue/produits/2026/09/produit_GJzjrY5.png",
        "categorie": 8,
        "categorie_nom": "Beauté",
        "boutique": 14,
        "boutique_nom": "Karité Doré",
        "boutique_slug": "karite-dore",
        "en_stock": True,
        "date_creation": "2026-09-27T20:27:03.031043Z",
    }


def test_product_without_category_image_or_price(client, db):
    """Sans catégorie : `categorie_nom` null (Django omet la clé) ; sans
    image : null ; sans prix de variante : prix de base (Django)."""
    db.search_rows = [product_row(categorie_id=None, categorie_nom=None, image_principale=None, prix_min=None)]
    (item,) = client.get("/recherche/produits").json()["results"]
    assert item["categorie"] is None and item["categorie_nom"] is None
    assert item["image_principale"] is None
    assert item["prix_min"] == 3500.0


@pytest.mark.parametrize("settings", [{"media_base_url": "https://cdn.anitche.test/media"}], indirect=True)
def test_image_url_uses_media_base_url_and_django_encoding(client, db):
    db.search_rows = [product_row(image_principale="catalogue/produits/photo été (1).png")]
    (item,) = client.get("/recherche/produits").json()["results"]
    assert item["image_principale"] == "https://cdn.anitche.test/media/catalogue/produits/photo%20%C3%A9t%C3%A9%20(1).png"


@pytest.mark.parametrize("settings", [{"public_base_url": "https://anitche.test/fast"}], indirect=True)
def test_next_and_previous_links_use_public_base_url_not_host(client, db):
    """Décision 10 : liens absolus construits avec PUBLIC_BASE_URL, filtres
    validés conservés (triés, comme DRF), jamais l'en-tête Host."""
    db.search_count = 45
    db.search_rows = [product_row()]
    response = client.get(
        "/recherche/produits?recherche=karit%C3%A9%20bio&categorie=beaute&prix_max=9000&tri=prix_asc&page=2&q=ignore",
        headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "http"},
    )
    body = response.json()
    base = "https://anitche.test/fast/recherche/produits"
    assert body["next"] == f"{base}?categorie=beaute&page=3&prix_max=9000&recherche=karit%C3%A9+bio&tri=prix_asc"
    assert body["previous"] == f"{base}?categorie=beaute&prix_max=9000&recherche=karit%C3%A9+bio&tri=prix_asc"
    assert body["count"] == 45 and body["facettes"] is None


def test_last_page_has_no_next_and_page_beyond_is_404(client, db):
    """Diagnostic 4 et C : au-delà de la dernière page, 404 « Page non
    valide. » (texte de Django) avec un code machine, sans lire la page."""
    db.search_count = 21
    assert client.get("/recherche/produits?page=2").json()["next"] is None
    response = client.get("/recherche/produits?page=3")
    assert response.status_code == 404
    assert response.json() == {
        "success": False, "status_code": 404, "detail": "Page non valide.", "errors": {"code": ["page_invalide"]},
    }
    assert len(db.search_calls("page")) == 1


def test_empty_catalogue_first_page_is_200(client, db):
    body = client.get("/recherche/produits?recherche=zzzz").json()
    assert body == {
        "count": 0, "next": None, "previous": None, "results": [],
        "facettes": {"categories": [], "boutiques": [], "prix": {"min": None, "max": None, "tranches": []}},
    }


def test_facets_are_read_from_the_summary_query(client, db):
    """Diagnostic 5 : facettes calculées par PostgreSQL sur l'ensemble filtré
    (tranches de prix : min inclus, max exclu, la dernière sans maximum)."""
    db.search_summary = {
        "total": 7,
        "prix_min": 1500,
        "prix_max": 120000,
        "tranches": '[{"tranche": 0, "nombre": 3}, {"tranche": 2, "nombre": 3}, {"tranche": 5, "nombre": 1}]',
        "categories": '[{"id": 8, "nom": "Beauté", "slug": "beaute", "parent": null, "nombre": 7}]',
        "boutiques": '[{"id": 14, "nom": "Karité Doré", "slug": "karite-dore", "nombre": 7}]',
    }
    body = client.get("/recherche/produits?recherche=karite").json()
    assert body["count"] == 7
    assert body["facettes"] == {
        "categories": [{"id": 8, "nom": "Beauté", "slug": "beaute", "parent": None, "nombre": 7}],
        "boutiques": [{"id": 14, "nom": "Karité Doré", "slug": "karite-dore", "nombre": 7}],
        "prix": {
            "min": 1500.0,
            "max": 120000.0,
            "tranches": [
                {"min": 0, "max": 5000, "nombre": 3},
                {"min": 10000, "max": 25000, "nombre": 3},
                {"min": 100000, "max": None, "nombre": 1},
            ],
        },
    }
    ((sql, _),) = db.search_calls("resume")
    assert "width_bucket(prix_min, ARRAY[5000, 10000, 25000, 50000, 100000]::numeric[])" in sql
    assert "LIMIT 20) AS c" in sql and "LIMIT 10) AS b" in sql


def test_suggestions_response(client, db):
    """Diagnostic 11-12 : types réels (catégorie, boutique, produit), id et
    slug pour naviguer ; plus de score ni de type « artisanat »."""
    db.suggestion_rows = [
        {"type": "categorie", "texte": "Beauté", "id": 8, "slug": "beaute"},
        {"type": "boutique", "texte": "Karité Doré", "id": 14, "slug": "karite-dore"},
        {"type": "produit", "texte": "Beurre de karité pur", "id": 41, "slug": "beurre-de-karite-pur-3d85cf"},
    ]
    body = client.get("/recherche/suggestions?recherche=%20%20Kari%20").json()
    assert body["requete"] == "Kari"
    assert [s["type"] for s in body["suggestions"]] == ["categorie", "boutique", "produit"]
    assert all(set(s) == {"type", "texte", "id", "slug"} for s in body["suggestions"])
    ((sql, params),) = db.search_calls("suggestions")
    assert params == ("Kari", 8)
    assert "LIMIT 2)" in sql and sql.rstrip().endswith("LIMIT $2")


# ------------------------------------------------------------ cache


def cache_keys(redis_server, pattern="fastapi:cache:*") -> list[str]:
    return sorted(fakeredis.FakeRedis(server=redis_server, decode_responses=True).keys(pattern))


def test_second_call_reads_facets_and_total_from_cache_but_never_the_results(client, db, redis_server):
    """Décision 6 : 2e appel identique sans requête SQL pour les facettes ;
    la page de résultats est relue à chaque appel (un produit masqué dans
    Django disparaît à la requête suivante)."""
    db.search_rows = [product_row(id=1), product_row(id=2)]
    first = client.get("/recherche/produits?recherche=karite").json()
    db.search_rows = [product_row(id=1)]  # un produit masqué entre-temps
    second = client.get("/recherche/produits?recherche=karite").json()

    assert len(db.search_calls("resume")) == 1
    assert len(db.search_calls("page")) == 2
    assert [p["id"] for p in first["results"]] == [1, 2]
    assert [p["id"] for p in second["results"]] == [1]  # résultats frais
    assert second["count"] == 2  # total en cache : jusqu'à 60 s de retard
    (key,) = cache_keys(redis_server)
    assert key.startswith("fastapi:cache:recherche:facettes:v1:") and "karite" not in key
    assert 0 < fakeredis.FakeRedis(server=redis_server).ttl(key) <= 60


def test_cache_depends_on_filters_not_on_sort_or_page(client, db, redis_server):
    db.search_count = 45
    client.get("/recherche/produits?recherche=karite")
    client.get("/recherche/produits?recherche=karite&tri=prix_desc")
    client.get("/recherche/produits?recherche=karite&page=2")  # total repris des facettes
    client.get("/recherche/produits?recherche=karite&categorie=beaute")
    assert len(db.search_calls("resume")) == 2
    assert db.search_calls("total") == []
    assert len(cache_keys(redis_server, "fastapi:cache:recherche:facettes:*")) == 2
    assert len(cache_keys(redis_server, "fastapi:cache:recherche:total:*")) == 1


def test_total_of_following_pages_is_cached(client, db, redis_server):
    db.search_count = 45
    client.get("/recherche/produits?page=2")
    client.get("/recherche/produits?page=3")
    assert len(db.search_calls("total")) == 1
    assert db.search_calls("resume") == []


def test_suggestions_are_cached(client, db, redis_server):
    db.suggestion_rows = [{"type": "produit", "texte": "Chemise en wax", "id": 32, "slug": "chemise-en-wax-a9808e"}]
    first = client.get("/recherche/suggestions?recherche=chem").json()
    db.suggestion_rows = []
    second = client.get("/recherche/suggestions?recherche=chem").json()
    assert first == second and len(db.search_calls("suggestions")) == 1
    client.get("/recherche/suggestions?recherche=chem&limite=3")
    client.get("/recherche/suggestions?recherche=Chem")  # casse différente : autre clé
    assert len(db.search_calls("suggestions")) == 3
    assert all(0 < fakeredis.FakeRedis(server=redis_server).ttl(key) <= 60 for key in cache_keys(redis_server))


@pytest.mark.parametrize("settings", [{"search_cache_ttl": 0}], indirect=True)
def test_cache_can_be_disabled(client, db, redis_server):
    client.get("/recherche/produits")
    client.get("/recherche/produits")
    client.get("/recherche/suggestions?recherche=chem")
    client.get("/recherche/suggestions?recherche=chem")
    assert len(db.search_calls("resume")) == 2 and len(db.search_calls("suggestions")) == 2
    assert cache_keys(redis_server) == []


def test_cache_failure_falls_back_to_the_database(client, db, caplog):
    """Même comportement que le socle : le cache est facultatif (lecture ou
    écriture impossible : calcul direct, journalisé) ; la limite de débit,
    elle, répond 503 si Redis est en panne (test suivant)."""
    redis = client.app.state.redis

    async def broken(*args, **kwargs):
        raise RedisConnectionError("panne")

    redis.get = broken
    redis.set = broken
    db.search_rows = [product_row()]
    with caplog.at_level(logging.WARNING, logger="anitche.fastapi.cache"):
        assert client.get("/recherche/produits").status_code == 200
        assert client.get("/recherche/suggestions?recherche=chem").status_code == 200
    assert "Cache indisponible (lecture) : ConnectionError" in caplog.text
    assert "Cache indisponible (écriture) : ConnectionError" in caplog.text


def test_redis_down_refuses_like_the_rate_limit(client, redis_server):
    redis_server.connected = False
    for path in ("/recherche/produits", "/recherche/suggestions?recherche=chem"):
        response = client.get(path)
        assert response.status_code == 503
        assert response.json()["detail"] == "Service temporairement indisponible."


def test_corrupted_cache_entry_is_recomputed(client, db, redis_server):
    client.get("/recherche/produits")
    raw = fakeredis.FakeRedis(server=redis_server, decode_responses=True)
    (key,) = raw.keys("fastapi:cache:*")
    raw.set(key, "{pas du json", ex=60)
    assert client.get("/recherche/produits").status_code == 200
    assert len(db.search_calls("resume")) == 2


# ------------------------------------------------------------ pannes


@pytest.mark.parametrize(
    "error",
    [
        asyncpg.InsufficientPrivilegeError("permission denied for view catalogue_produit_public"),
        asyncpg.UndefinedTableError('relation "catalogue_produit_public" does not exist'),
        asyncpg.QueryCanceledError("canceling statement due to statement timeout"),
        asyncpg.InterfaceError("pool is closing"),
        OSError("connexion refusée"),
        TimeoutError(),
    ],
    ids=["droit", "vue absente", "statement_timeout", "pool", "réseau", "délai"],
)
@pytest.mark.parametrize("path", ["/recherche/produits?recherche=wax", "/recherche/suggestions?recherche=wax"])
def test_database_failures_are_503_without_detail(client, db, caplog, error, path):
    db.error = error
    with caplog.at_level(logging.ERROR, logger="anitche.fastapi.search"):
        response = client.get(path)
    assert response.status_code == 503
    assert response.json() == {
        "success": False, "status_code": 503, "detail": "Service temporairement indisponible.", "errors": {},
    }
    assert "catalogue_produit_public" not in response.text
    assert f"Recherche impossible (PostgreSQL) : {type(error).__name__}" in caplog.text


def test_pool_forces_custom_plans_and_stays_read_only():
    """Plan recalculé à chaque exécution (le bon plan dépend du texte
    cherché) ; lecture seule et délai maximal inchangés."""
    assert DATABASE_SERVER_SETTINGS["plan_cache_mode"] == "force_custom_plan"
    assert DATABASE_SERVER_SETTINGS["default_transaction_read_only"] == "on"
    assert DATABASE_SERVER_SETTINGS["statement_timeout"] == "3000"


# ------------------------------------------------------------ non-régression


def test_no_hardcoded_catalogue_nor_global_state():
    """Diagnostic 1-2 : maquette CATALOGUE_INDEX supprimée ; le conseiller IA
    (module 4) garde sa propre maquette, immuable."""
    with pytest.raises(ModuleNotFoundError):
        __import__("app.services.recherche_service")
    assert not hasattr(search, "CATALOGUE_INDEX") and not hasattr(ia_service, "CATALOGUE_INDEX")
    assert isinstance(ia_service.MOCK_PRODUCTS, tuple)
    assert all(isinstance(product, MappingProxyType) for product in ia_service.MOCK_PRODUCTS)
    with pytest.raises(TypeError):
        ia_service.MOCK_PRODUCTS[0]["_score"] = 1


def test_ai_advisor_still_works_after_searches(client):
    client.get("/recherche/produits")
    response = client.post("/ia/recommandations", json={"budget_max": 40000.0})
    assert response.status_code == 200
    assert len(response.json()["recommandations"]) == 4
    assert all("_score" not in product for product in ia_service.MOCK_PRODUCTS)


def _app_with(db: FakePool):
    settings = make_settings()
    http = httpx.AsyncClient(base_url=settings.django_api_base_url, transport=httpx.MockTransport(FakeDjango()))
    return create_app(settings, resources=Resources(http=http, db=db, redis=fakeredis.FakeAsyncRedis(decode_responses=True)))


def test_two_applications_share_nothing():
    """Deux workers : chacun ses données (plus d'index en mémoire partagé)."""
    first_db, second_db = FakePool(), FakePool()
    first_db.search_rows = [product_row(id=1)]
    second_db.search_rows = [product_row(id=2), product_row(id=3)]
    with TestClient(_app_with(first_db)) as first, TestClient(_app_with(second_db)) as second:
        assert [p["id"] for p in first.get("/recherche/produits").json()["results"]] == [1]
        assert [p["id"] for p in second.get("/recherche/produits").json()["results"]] == [2, 3]
        assert [p["id"] for p in first.get("/recherche/produits").json()["results"]] == [1]


def test_routes_are_documented_with_the_common_error_schema(client):
    schema = client.get("/openapi.json").json()
    for path in ("/recherche/produits", "/recherche/suggestions"):
        responses = schema["paths"][path]["get"]["responses"]
        assert "422" not in responses
        assert responses["400"]["content"]["application/json"]["schema"]["$ref"].endswith("/Error")
    parameters = {p["name"] for p in schema["paths"]["/recherche/produits"]["get"]["parameters"]}
    assert parameters == {"recherche", "categorie", "boutique", "prix_min", "prix_max", "tri", "page"}
