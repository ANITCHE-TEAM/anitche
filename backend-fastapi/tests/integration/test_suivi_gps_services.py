"""Intégration PostgreSQL + Redis : suivi GPS de bout en bout.

Vraies ressources (pool du rôle en lecture seule, Redis base 15), deux
applications distinctes (deux workers ou deux instances), Django simulé.
"""
import time

import pytest
from fastapi.testclient import TestClient
from redis import Redis
from starlette.websockets import WebSocketDisconnect

pytestmark = pytest.mark.integration

POINT = (5.397340, -3.986620)
POSITION = {"latitude": 5.3200, "longitude": -4.0150, "vitesse_kmh": 30.0, "cap_degres": 45.0}


def headers(name: str) -> dict:
    return {"Authorization": f"Bearer jeton.{name}.it"}


def authenticate(ws, name: str) -> dict:
    ws.send_json({"type": "auth", "token": f"jeton.{name}.it"})
    return ws.receive_json()


def wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def numsub(redis_url: str, channel: str) -> int:
    with Redis.from_url(redis_url) as redis:
        return redis.pubsub_numsub(channel)[0][1]


def test_pubsub_between_two_applications_and_cleanup(seed, make_app, clean_redis):
    """Publication sur A, réception sur deux WebSockets de B. Un seul
    abonnement Redis pour B, retiré à la dernière déconnexion. TTL réel."""
    delivery_id = seed.delivery(client="client", courier="livreur", status="en_cours", point=POINT)
    channel = f"fastapi:gps:v1:chan:{delivery_id}"

    with TestClient(make_app()) as app_a, TestClient(make_app()) as app_b:
        with app_b.websocket_connect(f"/livraison/ws/{delivery_id}") as ws_b, \
                app_b.websocket_connect(f"/livraison/ws/{delivery_id}") as ws_c:
            assert authenticate(ws_b, "client")["type"] == "authentifie"
            assert authenticate(ws_c, "admin")["type"] == "authentifie"
            assert numsub(clean_redis, channel) == 1  # un abonnement par processus

            response = app_a.post("/livraison/position", json={"livraison_id": str(delivery_id), **POSITION}, headers=headers("livreur"))
            assert response.status_code == 200, response.text

            for ws in (ws_b, ws_c):
                message = ws.receive_json()
                assert message["type"] == "position"
                assert message["latitude"] == POSITION["latitude"]
                assert message["distance_restante_km"] > 0
                assert "livreur_id" not in message

        assert wait_until(lambda: numsub(clean_redis, channel) == 0)

    with Redis.from_url(clean_redis) as redis:
        assert 110 < redis.ttl(f"fastapi:gps:v1:pos:{delivery_id}") <= 120


def test_full_path_until_delivered(seed, make_app, clean_redis):
    """POST du livreur, GET du client (distance et ETA), WebSocket du client,
    passage à « livree » par le compte administrateur : fin_suivi puis 1000
    à la revalidation, position supprimée."""
    delivery_id = seed.delivery(client="client", courier="livreur", status="en_cours", point=POINT)

    with TestClient(make_app(ws_revalidate_interval=0.3)) as client:
        posted = client.post("/livraison/position", json={"livraison_id": str(delivery_id), **POSITION}, headers=headers("livreur"))
        assert posted.status_code == 200, posted.text

        read = client.get(f"/livraison/position/{delivery_id}", headers=headers("client"))
        assert read.status_code == 200
        body = read.json()
        assert body["distance_restante_km"] == 12.8 and body["temps_estime_minutes"] == 39

        third = client.get(f"/livraison/position/{delivery_id}", headers=headers("tiers"))
        assert third.status_code == 404 and third.json()["errors"] == {"code": ["livraison_introuvable"]}

        with client.websocket_connect(f"/livraison/ws/{delivery_id}") as ws:
            assert authenticate(ws, "client")["type"] == "authentifie"
            assert ws.receive_json()["type"] == "position"
            seed.set_status(delivery_id, "livree")
            assert ws.receive_json() == {"type": "fin_suivi", "livraison_id": str(delivery_id), "statut": "livree"}
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
            assert closed.value.code == 1000

        after = client.get(f"/livraison/position/{delivery_id}", headers=headers("client"))
        assert after.status_code == 404 and after.json()["errors"] == {"code": ["aucune_position"]}
        refused = client.post("/livraison/position", json={"livraison_id": str(delivery_id), **POSITION}, headers=headers("livreur"))
        assert refused.status_code == 409 and refused.json()["errors"] == {"code": ["livraison_pas_en_cours"]}

    with Redis.from_url(clean_redis) as redis:
        assert redis.exists(f"fastapi:gps:v1:pos:{delivery_id}") == 0


def test_without_client_point_eta_is_null(seed, make_app):
    delivery_id = seed.delivery(client="client", courier="livreur", status="en_cours", point=None)
    with TestClient(make_app()) as client:
        body = client.post("/livraison/position", json={"livraison_id": str(delivery_id), **POSITION}, headers=headers("livreur")).json()
    assert body["distance_restante_km"] is None and body["temps_estime_minutes"] is None


def test_courier_and_vendor_refusals_on_real_data(seed, make_app):
    delivery_id = seed.delivery(client="client", courier="livreur", status="en_cours", point=POINT)
    payload = {"livraison_id": str(delivery_id), **POSITION}
    with TestClient(make_app()) as client:
        other = client.post("/livraison/position", json=payload, headers=headers("autre_livreur"))
        assert other.status_code == 403 and other.json()["errors"] == {"code": ["livraison_non_assignee"]}
        admin = client.post("/livraison/position", json=payload, headers=headers("admin"))
        assert admin.status_code == 403 and admin.json()["errors"] == {"code": ["acces_reserve_livreurs"]}
        vendor = client.get(f"/livraison/position/{delivery_id}", headers=headers("vendeur"))
        assert vendor.status_code == 404 and vendor.json()["errors"] == {"code": ["livraison_introuvable"]}
        with client.websocket_connect(f"/livraison/ws/{delivery_id}") as ws:
            ws.send_json({"type": "auth", "token": "jeton.vendeur.it"})
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
        assert closed.value.code == 4403
