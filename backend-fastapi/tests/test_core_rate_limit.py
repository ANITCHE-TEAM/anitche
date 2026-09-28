"""Module 0 : limites de débit Redis (app/core/rate_limit.py).

Failles du rapport module 0 §1 couvertes : 18 (aucune limite sur les
routes publiques), 19 (aucune limite sur l'IA ; la limite de taille des
champs est traitée au module 4).
"""
import fakeredis
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core import rate_limit as rate_limit_module
from app.core.rate_limit import rate_limit
from tests.fakes import AUTH_HEADERS, TOKEN

ONE_PER_MINUTE = {name: "1/minute" for name in rate_limit_module.DEFAULT_RATE_LIMITS}
LIVRAISON_ID = "550e8400-e29b-41d4-a716-446655440000"
GPS_PAYLOAD = {"livraison_id": LIVRAISON_ID, "latitude": 5.33, "longitude": -4.0}

# Chaque route existante et son scope (rapport module 0, §2 d).
ROUTES = [
    ("get", "/recherche/produits", {}, "search"),
    ("get", "/recherche/suggestions?q=wax", {}, "suggestions"),
    ("post", "/qr/scan", {"json": {"qr_data": "PAS-2026-TIASSALE01"}}, "qr_scan"),
    ("get", "/qr/passeport/PAS-2026-MASQUE03", {}, "qr_scan"),
    ("post", "/ia/conseil", {"json": {"messages": [{"role": "user", "contenu": "Bonjour"}]}}, "ai_advice"),
    ("post", "/ia/recommandations", {"json": {}}, "ai_advice"),
    ("post", "/livraison/position", {"json": GPS_PAYLOAD, "headers": AUTH_HEADERS}, "gps_publish"),
]
# La lecture de la position (module 1) a son propre scope, testé seul : la
# première lecture exige une position publiée (test_suivi_gps.py, faille 11).


@pytest.fixture
def gps_delivery(db):
    """Module 1 : livraison en cours du livreur 42 (jeton par défaut), pour
    que la première publication et la première lecture réussissent."""
    db.add_delivery(LIVRAISON_ID, status="en_cours", courier_id=42, client_id=7)
    db.add_user(42, "livreur")


@pytest.mark.parametrize("settings", [{"rate_limits": ONE_PER_MINUTE}], indirect=True)
@pytest.mark.parametrize("method, path, kwargs, scope", ROUTES, ids=[f"{r[0]} {r[1]}" for r in ROUTES])
def test_existing_routes_are_rate_limited(client, redis_server, gps_delivery, method, path, kwargs, scope):
    """Faille 18-19 : chaque route existante a son scope ; au-delà du débit,
    429 au format commun avec Retry-After."""
    first = getattr(client, method)(path, **kwargs)
    assert first.status_code == 200, first.text
    second = getattr(client, method)(path, **kwargs)

    assert second.status_code == 429
    retry_after = int(second.headers["Retry-After"])
    assert 1 <= retry_after <= 60
    body = second.json()
    assert body["success"] is False and body["status_code"] == 429 and body["errors"] == {}
    unit = "seconde" if retry_after == 1 else "secondes"
    assert body["detail"] == f"Requête ralentie. Disponible à nouveau dans {retry_after} {unit}."

    keys = fakeredis.FakeRedis(server=redis_server, decode_responses=True).keys("fastapi:rl:*")
    assert [key.split(":")[2] for key in keys] == [scope]


@pytest.mark.parametrize("settings", [{"rate_limits": {"ws_connect": "1/minute"}}], indirect=True)
def test_websocket_connections_are_rate_limited_per_user(client, gps_delivery):
    # Module 1 : jeton dans le premier message.
    with client.websocket_connect(f"/livraison/ws/{LIVRAISON_ID}") as websocket:
        websocket.send_json({"type": "auth", "token": TOKEN})
        assert websocket.receive_json()["type"] == "authentifie"

    with client.websocket_connect(f"/livraison/ws/{LIVRAISON_ID}") as websocket:
        websocket.send_json({"type": "auth", "token": TOKEN})
        with pytest.raises(WebSocketDisconnect) as error:
            websocket.receive_text()
    assert error.value.code == 4429


@pytest.fixture
def app(app):
    """Routes de test : limite par IP et par utilisateur, sans logique métier."""

    @app.get("/_test/ip", dependencies=[Depends(rate_limit("search"))])
    async def by_ip():
        return {}

    @app.get("/_test/user", dependencies=[Depends(rate_limit("ai_advice", key="user"))])
    async def by_user():
        return {}

    return app


@pytest.mark.parametrize(
    "settings", [{"rate_limits": {"search": "2/minute"}, "trusted_proxy_count": 1}], indirect=True
)
def test_ip_limit_counts_each_client_separately(client, redis_server):
    first_ip = {"X-Forwarded-For": "203.0.113.1"}
    other_ip = {"X-Forwarded-For": "203.0.113.2"}
    assert [client.get("/_test/ip", headers=first_ip).status_code for _ in range(3)] == [200, 200, 429]
    assert client.get("/_test/ip", headers=other_ip).status_code == 200

    keys = sorted(fakeredis.FakeRedis(server=redis_server, decode_responses=True).keys("fastapi:rl:*"))
    assert [key.rsplit(":", 1)[0] for key in keys] == [
        "fastapi:rl:search:ip:203.0.113.1",
        "fastapi:rl:search:ip:203.0.113.2",
    ]


@pytest.mark.parametrize("settings", [{"rate_limits": {"search": "1/minute"}}], indirect=True)
def test_spoofed_forwarded_for_is_ignored_without_trusted_proxy(client):
    """Sans proxy de confiance, changer X-Forwarded-For ne contourne pas la limite."""
    assert client.get("/_test/ip", headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 200
    assert client.get("/_test/ip", headers={"X-Forwarded-For": "198.51.100.2"}).status_code == 429


@pytest.mark.parametrize("settings", [{"rate_limits": {"ai_advice": "1/minute"}}], indirect=True)
def test_user_limit_is_per_user_and_requires_authentication(client, django, redis_server):
    unauthenticated = client.get("/_test/user")
    assert unauthenticated.status_code == 401
    assert fakeredis.FakeRedis(server=redis_server).keys("fastapi:rl:*") == []

    assert client.get("/_test/user", headers=AUTH_HEADERS).status_code == 200
    assert client.get("/_test/user", headers=AUTH_HEADERS).status_code == 429

    # Autre utilisateur (autre jeton) : compteur distinct.
    django.json = {"id": 7, "role": "client"}
    other = {"Authorization": "Bearer autre.jeton.valide"}
    assert client.get("/_test/user", headers=other).status_code == 200
    keys = fakeredis.FakeRedis(server=redis_server, decode_responses=True).keys("fastapi:rl:*")
    assert sorted(key.split(":")[3] + ":" + key.split(":")[4] for key in keys) == ["user:42", "user:7"]


@pytest.mark.parametrize("settings", [{"rate_limits": {"search": "1/minute"}}], indirect=True)
def test_new_window_resets_the_counter(client, monkeypatch):
    now = 1_000_040.0  # fenêtre de 60 s commencée à 1_000_020
    monkeypatch.setattr(rate_limit_module.time, "time", lambda: now)
    assert client.get("/_test/ip").status_code == 200
    response = client.get("/_test/ip")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "40"

    now += 40
    assert client.get("/_test/ip").status_code == 200


def test_redis_down_refuses_with_503(client, redis_server):
    """Redis indisponible : refus (503), jamais un passage sans limite."""
    redis_server.connected = False
    response = client.get("/_test/ip")
    assert response.status_code == 503
    assert response.json() == {
        "success": False,
        "status_code": 503,
        "detail": "Service temporairement indisponible.",
        "errors": {},
    }


def test_unknown_scope_is_refused_at_declaration():
    with pytest.raises(ValueError):
        rate_limit("typo")


def test_counter_keys_expire(client, redis_server):
    client.get("/_test/ip")
    raw = fakeredis.FakeRedis(server=redis_server, decode_responses=True)
    (key,) = raw.keys("fastapi:rl:*")
    assert 0 < raw.ttl(key) <= 3600


@pytest.mark.parametrize("settings", [{"trusted_proxy_count": 1}], indirect=True)
def test_rate_limit_counts_the_real_client_behind_nginx(app, redis_server):
    """Derrière nginx (TRUSTED_PROXY_COUNT=1), l'IP comptée est celle posée
    par nginx dans X-Forwarded-For, pas l'adresse TCP de nginx."""
    with TestClient(app, client=("172.18.0.5", 40000)) as client:
        client.get("/_test/ip", headers={"X-Forwarded-For": "203.0.113.9"})
    (key,) = fakeredis.FakeRedis(server=redis_server, decode_responses=True).keys("fastapi:rl:*")
    assert key.startswith("fastapi:rl:search:ip:203.0.113.9:")
