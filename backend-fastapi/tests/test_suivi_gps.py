"""Module 1 : suivi GPS (routeur, Redis, WebSocket).

Un test (au moins) par faille du rapport module 1 §1 : 1 à 11, A, B, C.
Django simulé (un utilisateur par jeton), PostgreSQL simulé (FakePool émule
la requête d'accès), Redis : fakeredis. Les règles seules sont testées dans
test_suivi_gps_regles.py, les vrais services dans tests/integration/.
"""
import asyncio
import json
import logging
import math
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.core.resources import Resources
from app.main import create_app
from app.routeurs.suivi_temps_reel import _serve
from app.services import eta
from app.services.tracking import Subscription
from tests.fakes import FakePool, make_settings

DELIVERY = "11111111-1111-4111-8111-111111111111"
OTHER_DELIVERY = "22222222-2222-4222-8222-222222222222"
MISSING_DELIVERY = "33333333-3333-4333-8333-333333333333"
DELIVERY_UUID = UUID(DELIVERY)
# Point du client (Angré, Cocody) et position du livreur (Plateau).
DESTINATION = (5.397340, -3.986620)
POSITION = {"livraison_id": DELIVERY, "latitude": 5.3200, "longitude": -4.0150, "vitesse_kmh": 32.5, "cap_degres": 120.0}

# nom -> (id, rôle dans Django et en base)
USERS = {
    "livreur": (42, "livreur"),
    "autre_livreur": (99, "livreur"),
    "client": (7, "client"),
    "tiers": (8, "client"),
    "admin": (1, "admin"),
    "super_admin": (2, "super_admin"),
    "vendeur": (5, "vendeur"),
}
STATUSES_NOT_IN_PROGRESS = ["en_attente", "expediee", "livree", "echouee", "annulee"]


def token(name: str) -> str:
    return f"jeton.{name}.test"


def headers(name: str) -> dict:
    return {"Authorization": f"Bearer {token(name)}"}


def error_code(response) -> str:
    return response.json()["errors"]["code"][0]


@pytest.fixture(autouse=True)
def world(django, db):
    """Utilisateurs connus de Django et de la base, une livraison en cours
    (livreur 42, client 7) avec le point GPS du client."""
    for name, (user_id, role) in USERS.items():
        django.users_by_token[token(name)] = {"id": user_id, "role": role}
        db.add_user(user_id, role)
    db.add_delivery(DELIVERY, destination=DESTINATION)


def raw_redis(redis_server):
    return fakeredis.FakeRedis(server=redis_server, decode_responses=True)


def publish(client, **overrides):
    return client.post("/livraison/position", json={**POSITION, **overrides}, headers=headers("livreur"))


def ws_url(delivery: str = DELIVERY) -> str:
    return f"/livraison/ws/{delivery}"


def authenticate(ws, name: str) -> None:
    ws.send_json({"type": "auth", "token": token(name)})


def close_code(ws) -> int:
    with pytest.raises(WebSocketDisconnect) as error:
        ws.receive_text()
    return error.value.code


def ws_close_code(client, name: str, delivery: str = DELIVERY) -> int:
    with client.websocket_connect(ws_url(delivery)) as ws:
        authenticate(ws, name)
        return close_code(ws)


def strict_json(text: str):
    """JSON strict : NaN et Infinity refusés (comme JSON.parse)."""

    def refuse(constant):
        raise ValueError(f"constante non JSON : {constant}")

    return json.loads(text, parse_constant=refuse)


# ------------------------------------------------------------ faille 1 et B


@pytest.mark.parametrize("name", ["client", "livreur", "admin", "super_admin"])
def test_1_allowed_readers_get_the_position(client, name):
    assert publish(client).status_code == 200
    response = client.get(f"/livraison/position/{DELIVERY}", headers=headers(name))
    assert response.status_code == 200
    assert response.json()["latitude"] == POSITION["latitude"]


@pytest.mark.parametrize("name", ["tiers", "autre_livreur", "vendeur"])
def test_1_B_other_accounts_get_404_livraison_introuvable(client, name):
    """Faille 1 et B : client tiers, livreur non assigné, vendeur de la
    boutique : 404 comme Django, sans révéler l'existence."""
    publish(client)
    response = client.get(f"/livraison/position/{DELIVERY}", headers=headers(name))
    assert response.status_code == 404
    assert response.json() == {
        "success": False,
        "status_code": 404,
        "detail": "Livraison introuvable.",
        "errors": {"code": ["livraison_introuvable"]},
    }


@pytest.mark.parametrize("name", ["tiers", "autre_livreur", "vendeur"])
def test_1_B_other_accounts_cannot_listen(client, name):
    publish(client)
    assert ws_close_code(client, name) == 4403


def test_1_missing_delivery_is_404_livraison_introuvable(client):
    response = client.get(f"/livraison/position/{MISSING_DELIVERY}", headers=headers("admin"))
    assert response.status_code == 404
    assert error_code(response) == "livraison_introuvable"


def test_B_vendor_reads_the_delivery_of_its_own_purchase(client, db):
    db.add_delivery(OTHER_DELIVERY, client_id=5)
    assert publish(client, livraison_id=OTHER_DELIVERY).status_code == 200
    response = client.get(f"/livraison/position/{OTHER_DELIVERY}", headers=headers("vendeur"))
    assert response.status_code == 200


# ------------------------------------------------------------ faille 2


@pytest.mark.parametrize("bad_id", ["../utilisateurs/profil", "../../admin/", "abc", "x" * 5000, "", 123])
def test_2_post_refuses_non_uuid_livraison_id(client, redis_server, bad_id):
    response = publish(client, livraison_id=bad_id)
    assert response.status_code == 400
    assert response.json()["errors"] == {"livraison_id": ["Doit être un UUID valide."]}
    assert raw_redis(redis_server).keys("fastapi:gps:*") == []


@pytest.mark.parametrize("bad_id", ["pas-un-uuid", "%2e%2e", "abc"])
def test_2_get_refuses_non_uuid(client, bad_id):
    response = client.get(f"/livraison/position/{bad_id}", headers=headers("admin"))
    assert response.status_code == 400
    assert "livraison_id" in response.json()["errors"]


def test_2_websocket_refuses_non_uuid_with_1008(client, django):
    with client.websocket_connect("/livraison/ws/pas-un-uuid") as ws:
        assert close_code(ws) == 1008
    assert django.requests == []  # refus avant toute authentification


# ------------------------------------------------------------ faille 3 et 5


def test_3_body_livreur_id_is_ignored(client, redis_server):
    """Le livreur est celui du jeton : livreur_id du corps ignoré."""
    response = publish(client, livreur_id=99)
    assert response.status_code == 200
    stored = json.loads(raw_redis(redis_server).get(f"fastapi:gps:v1:pos:{DELIVERY}"))
    assert stored["livreur_id"] == 42


def test_3_unassigned_courier_cannot_publish(client, redis_server):
    response = client.post("/livraison/position", json=POSITION, headers=headers("autre_livreur"))
    assert response.status_code == 403
    assert response.json()["detail"] == "Vous n'êtes pas autorisé à modifier cette livraison."
    assert error_code(response) == "livraison_non_assignee"
    assert raw_redis(redis_server).keys("fastapi:gps:*") == []


@pytest.mark.parametrize("name", ["admin", "super_admin", "client", "vendeur"])
def test_5_only_couriers_publish(client, db, name):
    """Faille 5 : l'administration ne publie pas (403), sans requête SQL."""
    response = client.post("/livraison/position", json=POSITION, headers=headers(name))
    assert response.status_code == 403
    assert response.json()["detail"] == "Accès réservé aux livreurs."
    assert error_code(response) == "acces_reserve_livreurs"
    assert db.queries == []


def test_5_removed_courier_is_refused_immediately(client, db):
    """Rôle lu en base : un livreur retiré (rôle client) est refusé même
    si son jeton en cache dit encore « livreur »."""
    db.add_user(42, "client")
    response = publish(client)
    assert response.status_code == 403
    assert error_code(response) == "acces_reserve_livreurs"


def test_5_deactivated_courier_is_refused(client, db):
    db.add_user(42, "livreur", is_active=False)
    assert error_code(publish(client)) == "acces_reserve_livreurs"


# ------------------------------------------------------------ faille 4


@pytest.mark.parametrize("status", STATUSES_NOT_IN_PROGRESS)
def test_4_publication_refused_outside_en_cours(client, db, redis_server, status):
    db.add_delivery(DELIVERY, status=status)
    response = publish(client)
    assert response.status_code == 409
    assert response.json()["detail"] == "La livraison n'est pas en cours de livraison."
    assert error_code(response) == "livraison_pas_en_cours"
    assert raw_redis(redis_server).keys("fastapi:gps:*") == []


def test_4_missing_delivery_is_404(client):
    response = publish(client, livraison_id=MISSING_DELIVERY)
    assert response.status_code == 404
    assert error_code(response) == "livraison_introuvable"


def test_4_access_is_read_in_postgres(client, db):
    publish(client)
    assert any("FROM livraison_livraison" in query for query in db.queries)


# ------------------------------------------------------------ faille 6


def test_6_no_position_is_404_never_a_fake_one(client):
    response = client.get(f"/livraison/position/{DELIVERY}", headers=headers("client"))
    assert response.status_code == 404
    assert response.json() == {
        "success": False,
        "status_code": 404,
        "detail": "Aucune position disponible pour cette livraison.",
        "errors": {"code": ["aucune_position"]},
    }


@pytest.mark.parametrize("status", STATUSES_NOT_IN_PROGRESS)
def test_6_no_position_outside_en_cours_and_key_deleted(client, db, redis_server, status):
    publish(client)
    db.add_delivery(DELIVERY, status=status)
    response = client.get(f"/livraison/position/{DELIVERY}", headers=headers("client"))
    assert response.status_code == 404
    assert error_code(response) == "aucune_position"
    assert raw_redis(redis_server).exists(f"fastapi:gps:v1:pos:{DELIVERY}") == 0


def test_6_position_of_a_previous_courier_is_never_shown(client, db, redis_server):
    publish(client)
    db.add_delivery(DELIVERY, courier_id=99, destination=DESTINATION)  # réassignation
    response = client.get(f"/livraison/position/{DELIVERY}", headers=headers("client"))
    assert error_code(response) == "aucune_position"
    assert raw_redis(redis_server).exists(f"fastapi:gps:v1:pos:{DELIVERY}") == 0


def test_6_expired_position_is_not_shown(client, redis_server):
    publish(client)
    raw_redis(redis_server).delete(f"fastapi:gps:v1:pos:{DELIVERY}")  # expiration
    assert error_code(client.get(f"/livraison/position/{DELIVERY}", headers=headers("client"))) == "aucune_position"


# ------------------------------------------------------------ faille 7


def test_7_position_key_has_a_ttl(client, redis_server):
    publish(client)
    ttl = raw_redis(redis_server).ttl(f"fastapi:gps:v1:pos:{DELIVERY}")
    assert 110 < ttl <= 120


def test_7_no_module_singleton():
    import app.services.tracking as tracking_module

    with pytest.raises(ImportError):
        import app.services.websocket_manager  # noqa: F401
    assert not [value for value in vars(tracking_module).values() if isinstance(value, tracking_module.TrackingHub)]


@pytest.fixture
def second_app(settings, django, redis_server, db):
    """Deuxième application (autre worker ou autre instance) sur le même
    Redis et la même base."""
    http = httpx.AsyncClient(base_url=settings.django_api_base_url, transport=httpx.MockTransport(django))
    redis = fakeredis.FakeAsyncRedis(server=redis_server, decode_responses=True)
    return create_app(settings, resources=Resources(http=http, db=db, redis=redis))


def test_7_position_is_shared_between_applications(client, second_app):
    publish(client)
    with TestClient(second_app) as other:
        response = other.get(f"/livraison/position/{DELIVERY}", headers=headers("client"))
    assert response.status_code == 200
    assert response.json()["latitude"] == POSITION["latitude"]


def test_7_websocket_on_another_application_receives_the_publication(client, second_app):
    with TestClient(second_app) as other:
        with other.websocket_connect(ws_url()) as ws:
            authenticate(ws, "client")
            assert ws.receive_json()["type"] == "authentifie"
            publish(client, latitude=5.33)
            message = ws.receive_json()
    assert message["type"] == "position"
    assert message["latitude"] == 5.33


# ------------------------------------------------------------ faille 8


@pytest.mark.parametrize("settings", [{"ws_auth_timeout": 0.2}], indirect=True)
def test_8_token_in_url_is_ignored_and_closes_4401(client, django):
    with client.websocket_connect(f"{ws_url()}?token={token('client')}") as ws:
        assert close_code(ws) == 4401
    assert django.requests == []


def test_8_first_message_authentication_opens_the_stream(client):
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "client")
        assert ws.receive_json() == {"type": "authentifie", "livraison_id": DELIVERY}


@pytest.mark.parametrize(
    "first_message",
    [
        "pas du json",
        json.dumps({"type": "position", "latitude": 1}),
        json.dumps({"type": "auth"}),
        json.dumps({"type": "auth", "token": ""}),
        json.dumps(["auth"]),
    ],
)
def test_8_invalid_first_message_closes_4401(client, first_message):
    with client.websocket_connect(ws_url()) as ws:
        ws.send_text(first_message)
        assert close_code(ws) == 4401


def test_8_binary_first_message_closes_4401(client):
    """Pendant l'authentification, tout autre message : 4401."""
    with client.websocket_connect(ws_url()) as ws:
        ws.send_bytes(b"\x00\x01")
        assert close_code(ws) == 4401


def test_10_binary_message_after_authentication_closes_1008(client):
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "client")
        ws.receive_json()
        ws.send_bytes(b"\x00\x01")
        assert close_code(ws) == 1008


@pytest.mark.parametrize("status_code, expected", [(401, 4401), (429, 4429), (503, 1011)])
def test_8_django_answer_sets_the_close_code(client, django, status_code, expected):
    django.status_code = status_code
    with client.websocket_connect(ws_url()) as ws:
        ws.send_json({"type": "auth", "token": "jeton.inconnu.test"})
        assert close_code(ws) == expected


@pytest.mark.parametrize("settings", [{"rate_limits": {"ws_connect": "1/minute"}}], indirect=True)
def test_8_connections_are_rate_limited_4429(client):
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "client")
        ws.receive_json()
    assert ws_close_code(client, "client") == 4429


def test_8_listener_outside_en_cours_closes_4403(client, db):
    db.add_delivery(DELIVERY, status="expediee")
    assert ws_close_code(client, "client") == 4403


def test_8_last_position_is_sent_after_authentication(client):
    publish(client)
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "admin")
        assert ws.receive_json()["type"] == "authentifie"
        message = ws.receive_json()
    assert message["type"] == "position" and message["latitude"] == POSITION["latitude"]


# ------------------------------------------------------------ faille 9


REVALIDATE_FAST = [{"ws_revalidate_interval": 0.1, "auth_cache_ttl": 0}]


@pytest.mark.parametrize("settings", REVALIDATE_FAST, indirect=True)
def test_9_revoked_token_closes_4401(client, django):
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "client")
        ws.receive_json()
        django.users_by_token[token("client")] = None
        assert close_code(ws) == 4401


@pytest.mark.parametrize("settings", REVALIDATE_FAST, indirect=True)
def test_9_delivered_sends_fin_suivi_then_1000(client, db, redis_server):
    publish(client)
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "client")
        assert ws.receive_json()["type"] == "authentifie"
        assert ws.receive_json()["type"] == "position"
        db.add_delivery(DELIVERY, status="livree")
        assert ws.receive_json() == {"type": "fin_suivi", "livraison_id": DELIVERY, "statut": "livree"}
        assert close_code(ws) == 1000
    assert raw_redis(redis_server).exists(f"fastapi:gps:v1:pos:{DELIVERY}") == 0


@pytest.mark.parametrize("settings", REVALIDATE_FAST, indirect=True)
def test_9_reassigned_courier_listening_closes_4403(client, db):
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "livreur")
        ws.receive_json()
        db.add_delivery(DELIVERY, courier_id=99)
        assert close_code(ws) == 4403


@pytest.mark.parametrize("settings", REVALIDATE_FAST, indirect=True)
def test_9_deactivated_account_closes_4403(client, db):
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "client")
        ws.receive_json()
        db.add_user(7, "client", is_active=False)
        assert close_code(ws) == 4403


@pytest.mark.parametrize("settings", REVALIDATE_FAST, indirect=True)
def test_9_database_down_during_revalidation_closes_1011(client, db):
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "client")
        ws.receive_json()
        db.error = OSError("connexion perdue")
        assert close_code(ws) == 1011


def test_9_token_renewal_keeps_the_connection(client, django):
    django.users_by_token["jeton.client.renouvele"] = {"id": 7, "role": "client"}
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "client")
        ws.receive_json()
        ws.send_json({"type": "auth", "token": "jeton.client.renouvele"})
        publish(client, latitude=5.34)
        message = ws.receive_json()
    assert message["type"] == "position" and message["latitude"] == 5.34


def test_9_renewal_with_another_account_closes_4401(client):
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "client")
        ws.receive_json()
        authenticate(ws, "admin")
        assert close_code(ws) == 4401


def test_9_revoked_renewal_token_closes_4401(client, django):
    django.users_by_token["jeton.revoque.test"] = None
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "client")
        ws.receive_json()
        ws.send_json({"type": "auth", "token": "jeton.revoque.test"})
        assert close_code(ws) == 4401


# ------------------------------------------------------------ faille 10 et 10 bis


@pytest.mark.parametrize("message", ['x", "type": "mise_a_jour_position", "latitude": 48.85, "z": "', '{"type": "ping"}', '"'])
def test_10_unexpected_message_closes_1008_without_echo(client, message):
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "client")
        ws.receive_json()
        ws.send_text(message)
        assert close_code(ws) == 1008


@pytest.mark.parametrize("authenticated", [False, True])
def test_10_message_over_4096_bytes_closes_1009(client, authenticated):
    with client.websocket_connect(ws_url()) as ws:
        if authenticated:
            authenticate(ws, "client")
            ws.receive_json()
        ws.send_text(json.dumps({"type": "auth", "token": "é" * 2100}))
        assert close_code(ws) == 1009


@pytest.mark.parametrize(
    "raw_body",
    [
        '{"livraison_id": "%s", "latitude": NaN, "longitude": -4.0}',
        '{"livraison_id": "%s", "latitude": 5.3, "longitude": Infinity}',
        '{"livraison_id": "%s", "latitude": 5.3, "longitude": -4.0, "vitesse_kmh": -Infinity}',
        '{"livraison_id": "%s", "latitude": "NaN", "longitude": -4.0}',
    ],
)
def test_10bis_nan_and_infinity_are_refused(client, redis_server, raw_body):
    response = client.post(
        "/livraison/position",
        content=raw_body % DELIVERY,
        headers={**headers("livreur"), "Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert raw_redis(redis_server).keys("fastapi:gps:*") == []


@pytest.mark.parametrize(
    "field, value",
    [("latitude", 90.1), ("latitude", -91), ("longitude", 180.5), ("vitesse_kmh", 250), ("vitesse_kmh", -1), ("cap_degres", 360), ("cap_degres", -5000)],
)
def test_10bis_values_are_bounded(client, field, value):
    response = publish(client, **{field: value})
    assert response.status_code == 400
    assert field in response.json()["errors"]


def test_10_every_message_sent_is_strict_json(client):
    publish(client)
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "client")
        texts = [ws.receive_text(), ws.receive_text()]
        publish(client, vitesse_kmh=None, cap_degres=None)
        texts.append(ws.receive_text())
    for text in texts:
        strict_json(text)


# ------------------------------------------------------------ faille 11


@pytest.mark.parametrize("settings", [{"rate_limits": {"gps_read": "2/minute"}}], indirect=True)
def test_11_reading_is_rate_limited(client, redis_server):
    publish(client)
    statuses = [client.get(f"/livraison/position/{DELIVERY}", headers=headers("client")).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
    response = client.get(f"/livraison/position/{DELIVERY}", headers=headers("client"))
    assert 1 <= int(response.headers["Retry-After"]) <= 60
    assert raw_redis(redis_server).keys("fastapi:rl:gps_read:user:7:*")


@pytest.mark.parametrize("settings", [{"ws_client_messages_per_minute": 2}], indirect=True)
def test_11_too_many_client_messages_close_4429(client):
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "client")
        ws.receive_json()
        for _ in range(3):
            authenticate(ws, "client")
        assert close_code(ws) == 4429


# ------------------------------------------------------------ constats A et C


def test_A_courier_id_is_never_sent(client):
    posted = publish(client)
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "client")
        messages = [ws.receive_json(), ws.receive_json()]
        publish(client)
        messages.append(ws.receive_json())
    read = client.get(f"/livraison/position/{DELIVERY}", headers=headers("client"))
    for body in [posted.json(), read.json(), *messages]:
        assert "livreur_id" not in body
        # Aucune valeur n'est l'identifiant du livreur (42), sous aucun nom.
        assert 42 not in body.values() and "42" not in body.values()


def test_C_timestamp_is_the_server_time_in_utc(client):
    response = publish(client, horodatage="2000-01-01 00:00:00")
    horodatage = response.json()["horodatage"]
    assert horodatage.endswith("Z") and not horodatage.startswith("2000")
    parsed = datetime.fromisoformat(horodatage)
    assert parsed.tzinfo is not None
    assert abs(datetime.now(UTC) - parsed) < timedelta(seconds=10)


def test_published_position_contract(client):
    response = publish(client)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "livraison_id", "latitude", "longitude", "vitesse_kmh", "cap_degres",
        "horodatage", "distance_restante_km", "temps_estime_minutes",
    }
    assert body["livraison_id"] == DELIVERY


# ------------------------------------------------------------ distance et ETA


def test_eta_with_the_client_point(client):
    body = publish(client).json()
    expected_km, expected_minutes = eta.estimate(
        POSITION["latitude"], POSITION["longitude"], DESTINATION, detour_factor=1.4, average_speed_kmh=20.0
    )
    assert body["distance_restante_km"] == expected_km and expected_km > 0
    assert body["temps_estime_minutes"] == expected_minutes and expected_minutes >= 1
    read = client.get(f"/livraison/position/{DELIVERY}", headers=headers("client")).json()
    assert (read["distance_restante_km"], read["temps_estime_minutes"]) == (expected_km, expected_minutes)


def test_eta_is_null_without_the_client_point(client, db):
    db.add_delivery(DELIVERY, destination=None)
    body = publish(client).json()
    assert body["distance_restante_km"] is None and body["temps_estime_minutes"] is None
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "admin")
        ws.receive_json()
        message = ws.receive_json()
    assert message["distance_restante_km"] is None and message["temps_estime_minutes"] is None
    read = client.get(f"/livraison/position/{DELIVERY}", headers=headers("client")).json()
    assert read["distance_restante_km"] is None and read["temps_estime_minutes"] is None


def test_eta_in_websocket_position_messages(client):
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "client")
        ws.receive_json()
        publish(client)
        message = ws.receive_json()
    assert isinstance(message["distance_restante_km"], float) and math.isfinite(message["distance_restante_km"])
    assert isinstance(message["temps_estime_minutes"], int) and message["temps_estime_minutes"] >= 1


@pytest.mark.parametrize("settings", [{"eta_detour_factor": 1.0, "eta_average_speed_kmh": 60.0}], indirect=True)
def test_eta_uses_the_settings(client):
    body = publish(client).json()
    assert body["distance_restante_km"] == round(eta.haversine_km(POSITION["latitude"], POSITION["longitude"], *DESTINATION), 1)


# ------------------------------------------------------------ pannes


def test_redis_down_is_503_for_publication_and_reading(client, redis_server):
    redis_server.connected = False
    for response in (publish(client), client.get(f"/livraison/position/{DELIVERY}", headers=headers("client"))):
        assert response.status_code == 503
        assert response.json()["detail"] == "Service temporairement indisponible."


def test_database_down_is_503(client, db):
    db.error = OSError("connexion refusée")
    assert publish(client).status_code == 503
    assert client.get(f"/livraison/position/{DELIVERY}", headers=headers("client")).status_code == 503
    assert ws_close_code(client, "client") == 1011


def test_redis_pubsub_lost_closes_listeners_1011(client, redis_server):
    with client.websocket_connect(ws_url()) as ws:
        authenticate(ws, "client")
        ws.receive_json()
        redis_server.connected = False
        assert close_code(ws) == 1011


# ------------------------------------------------------------ abonnements


def test_one_channel_subscription_per_process_and_cleanup(client, redis_server, app):
    hub = app.state.tracking_hub
    with client.websocket_connect(ws_url()) as first, client.websocket_connect(ws_url()) as second:
        authenticate(first, "client")
        authenticate(second, "admin")
        first.receive_json()
        second.receive_json()
        assert hub.listener_count(DELIVERY_UUID) == 2
        assert raw_redis(redis_server).pubsub_numsub(f"fastapi:gps:v1:chan:{DELIVERY}") == [
            (f"fastapi:gps:v1:chan:{DELIVERY}", 1)
        ]
        publish(client)
        assert first.receive_json()["type"] == "position"
        assert second.receive_json()["type"] == "position"
    # Dernière déconnexion : désabonnement du canal.
    for _ in range(50):
        if raw_redis(redis_server).pubsub_numsub(f"fastapi:gps:v1:chan:{DELIVERY}")[0][1] == 0:
            break
        time.sleep(0.02)
    assert hub.listener_count(DELIVERY_UUID) == 0
    assert raw_redis(redis_server).pubsub_numsub(f"fastapi:gps:v1:chan:{DELIVERY}")[0][1] == 0


def test_8_uvicorn_handshake_log_has_no_query_string(app):
    """Faille 8 (constat de bout en bout) : uvicorn écrit la poignée de main
    WebSocket avec la chaîne de requête, même avec --no-access-log. Le
    filtre installé par create_app la retire."""
    record = logging.LogRecord(
        "uvicorn.error", logging.INFO, __file__, 0, '%s - "WebSocket %s" [accepted]',
        ("172.19.0.1:58970", f"/livraison/ws/{DELIVERY}?token=eyJhbGciOiJIUzI1NiJ9.secret.sig"), None,
    )
    logger = logging.getLogger("uvicorn.error")
    assert all(log_filter.filter(record) for log_filter in logger.filters)
    assert record.getMessage() == f'172.19.0.1:58970 - "WebSocket /livraison/ws/{DELIVERY}" [accepted]'
    assert len(logger.filters) == len({type(log_filter) for log_filter in logger.filters})  # installé une fois


def test_external_cancellation_keeps_the_caller_cancelled_error():
    """Arrêt du serveur ou TestClient : la tâche du handler est annulée de
    l'extérieur, parfois plusieurs fois (anyio répète l'annulation). La
    CancelledError qui remonte doit rester celle de l'appelant, sinon sa
    portée d'annulation ne la reconnaît pas et la laisse fuir (constaté :
    tests WebSocket intermittents avec asyncio.gather)."""
    class SilentWebSocket:
        async def receive(self):
            await asyncio.Event().wait()

    connection = SimpleNamespace(
        websocket=SilentWebSocket(),
        settings=SimpleNamespace(ws_client_messages_per_minute=6, ws_max_message_bytes=4096, ws_revalidate_interval=60),
        message_times=deque(),
    )

    async def scenario():
        task = asyncio.create_task(_serve(connection, Subscription(DELIVERY_UUID, 8)))
        await asyncio.sleep(0.01)  # en attente dans asyncio.wait
        task.cancel("portee appelante")
        await asyncio.sleep(0)  # nettoyage des sous-tâches en cours
        task.cancel("portee appelante")
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await task
        return cancelled.value.args

    assert asyncio.run(scenario()) == ("portee appelante",)
