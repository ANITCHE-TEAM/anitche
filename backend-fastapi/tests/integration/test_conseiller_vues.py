"""Conseiller IA sur les VRAIES vues du catalogue (migration Django
catalogue 0004), avec le rôle en lecture seule et un vrai Redis.

Les candidats du conseiller passent par la recherche publique : un
produit masqué dans Django, en rupture ou hors budget n'est jamais
proposé, quel que soit le fournisseur. Chaque test isole ses produits dans
une catégorie créée pour lui (les termes du lexique, « bazin », « pagne »…,
sont communs).
"""
import asyncio
import sys
import types
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from redis.asyncio import Redis

from app.services.conseiller import fournisseurs as registre
from app.services.conseiller.fournisseurs.base import FournisseurIA
from tests.fakes import AUTH_HEADERS

pytestmark = pytest.mark.integration


def ids(response, key="produits_suggeres") -> list[int]:
    assert response.status_code == 200, response.text
    return [product["id"] for product in response.json()[key]]


def conseil(texte: str, **champs) -> dict:
    return {"messages": [{"role": "user", "contenu": texte}], **champs}


def test_hidden_out_of_stock_and_over_budget_products_are_never_proposed(catalogue, make_search_app):
    marker = catalogue.marker
    category = catalogue.category(f"Mode {marker}")
    shop = catalogue.shop(f"Atelier {marker}")
    robe = catalogue.product(f"Robe en bazin {marker}", shop=shop, category=category,
                             variants=((22000, None, True, 5),))
    pagne = catalogue.product(f"Pagne tissé {marker}", shop=shop, category=category, variants=((9500, None, True, 3),))
    hidden = {
        "rupture": catalogue.product(f"Boubou {marker}", shop=shop, category=category, variants=((15000, None, True, 0),)),
        "sans ligne de stock": catalogue.product(f"Boubou brodé {marker}", shop=shop, category=category,
                                                 variants=((15000, None, True, None),)),
        "désactivé": catalogue.product(f"Bazin inactif {marker}", shop=shop, category=category, active=False),
        "boutique suspendue": catalogue.product(f"Kita {marker}", category=category,
                                                shop=catalogue.shop(f"Suspendue {marker}", suspended=True)),
        "vendeur non validé": catalogue.product(f"Bazin brodé {marker}", category=category, shop=catalogue.shop(
            f"Attente {marker}", owner=catalogue.vendor(kyc="en_attente"))),
        "hors budget": catalogue.product(f"Pagne de luxe {marker}", shop=shop, category=category,
                                         variants=((50000, None, True, 2),)),
    }

    with TestClient(make_search_app()) as client:
        response = client.post("/ia/conseil", headers=AUTH_HEADERS, json=conseil(
            "Une tenue pour un mariage", budget_max=30000, categories=[category["slug"]],
        ))

    assert ids(response) == [robe, pagne]  # la robe utilise au moins la moitié du budget
    assert not set(ids(response)) & set(hidden.values())
    assert response.json()["source"] == "regles"


def test_real_display_price_is_compared_to_the_budget(catalogue, make_search_app):
    marker = catalogue.marker
    category = catalogue.category(f"Beaute {marker}")
    shop = catalogue.shop(f"Atelier {marker}")
    promo = catalogue.product(f"Savon {marker}", shop=shop, category=category, base_price=25000,
                              variants=((25000, 22000, True, 4),))

    with TestClient(make_search_app()) as client:
        within = client.post("/ia/recommandations", headers=AUTH_HEADERS,
                             json={"categories": [category["slug"]], "budget_max": 23000})
        below = client.post("/ia/recommandations", headers=AUTH_HEADERS,
                            json={"categories": [category["slug"]], "budget_max": 21000})

    assert ids(within, "recommandations") == [promo]
    assert within.json()["recommandations"][0]["prix_min"] == 22000.0
    assert ids(below, "recommandations") == []


def test_parent_category_includes_subcategories(catalogue, make_search_app):
    marker = catalogue.marker
    parent = catalogue.category(f"Maison {marker}")
    child = catalogue.category(f"Cuisine {marker}", parent=parent)
    shop = catalogue.shop(f"Atelier {marker}")
    nappe = catalogue.product(f"Nappe en kita {marker}", shop=shop, category=child)

    with TestClient(make_search_app()) as client:
        by_slug = client.post("/ia/recommandations", headers=AUTH_HEADERS, json={"categories": [parent["slug"]]})
        by_id = client.post("/ia/recommandations", headers=AUTH_HEADERS, json={"categories": [str(parent["id"])]})

    assert ids(by_slug, "recommandations") == ids(by_id, "recommandations") == [nappe]


def test_hiding_a_product_takes_effect_at_the_next_request(catalogue, make_search_app):
    """Aucun cache : boutique suspendue (compte admin) → produit absent des
    conseils dès la requête suivante."""
    marker = catalogue.marker
    category = catalogue.category(f"Mode {marker}")
    shop = catalogue.shop(f"Atelier {marker}")
    robe = catalogue.product(f"Robe en bazin {marker}", shop=shop, category=category)
    payload = conseil("mariage", categories=[category["slug"]])

    with TestClient(make_search_app()) as client:
        before = ids(client.post("/ia/conseil", headers=AUTH_HEADERS, json=payload))
        catalogue.execute("UPDATE vendeurs_boutique SET est_suspendue = true WHERE id = $1", shop["id"])
        after = ids(client.post("/ia/conseil", headers=AUTH_HEADERS, json=payload))

    assert before == [robe] and after == []


class FauxPayant(FournisseurIA):
    code = "faux_payant"

    async def conseiller(self, demande):
        return {"produits": [{"id": c.id, "justification": "Choisi."} for c in demande.candidats[:1]],
                "message": "Voici.", "conseils": []}


def test_daily_budget_guard_uses_a_real_redis_counter(catalogue, make_search_app, monkeypatch, clean_redis):
    module = types.ModuleType("tests_integration_faux_payant")
    module.creer = lambda settings: FauxPayant()
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(registre.FOURNISSEURS, "faux_payant", module.__name__)
    marker = catalogue.marker
    category = catalogue.category(f"Mode {marker}")
    robe = catalogue.product(f"Robe en bazin {marker}", shop=catalogue.shop(f"Atelier {marker}"), category=category)
    payload = conseil("mariage", categories=[category["slug"]])

    with TestClient(make_search_app(ai_provider="faux_payant", ai_daily_call_limit=2)) as client:
        responses = [client.post("/ia/conseil", headers=AUTH_HEADERS, json=payload).json() for _ in range(3)]

    assert [body["source"] for body in responses] == ["ia", "ia", "regles"]
    assert all([p["id"] for p in body["produits_suggeres"]] == [robe] for body in responses)

    async def counter():
        redis = Redis.from_url(clean_redis, decode_responses=True)
        try:
            key = "fastapi:ia:appels:" + datetime.now(UTC).strftime("%Y-%m-%d")
            return await redis.get(key), await redis.ttl(key)
        finally:
            await redis.aclose()

    value, ttl = asyncio.run(counter())
    assert value == "3" and 0 < ttl <= 2 * 86400
