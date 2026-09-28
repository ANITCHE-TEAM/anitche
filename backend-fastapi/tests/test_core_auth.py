"""Module 0 : authentification déléguée à Django (app/core/auth.py).

Le vrai code est exécuté : Django est simulé par httpx.MockTransport
(tests/fakes.py), Redis par fakeredis. Failles du rapport module 0 §1
couvertes : 8-13 (correspondance des réponses de Django), 16 (un client
httpx et 1 à 2 appels à Django par requête), 17 (délai dépassé).
"""
import hashlib
import json

import fakeredis
import httpx
import pytest
from fastapi import Depends
from starlette.websockets import WebSocketDisconnect

from app.core.auth import CurrentUser, get_current_user
from tests.fakes import AUTH_HEADERS, TOKEN, VERIFY_URL

PROTECTED = "/_test/me"


@pytest.fixture
def app(app):
    """Route de test protégée, sans limite de débit ni logique métier."""

    @app.get(PROTECTED)
    async def me(user: CurrentUser = Depends(get_current_user)):
        return {"id": user.id, "role": user.role}

    return app


def assert_401(response, detail: str) -> None:
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {"success": False, "status_code": 401, "detail": detail, "errors": {}}


# ------------------------------------------------ en-tête Authorization


def test_missing_authorization_header_is_401_not_403(client, django):
    """Faille 9 : 401 (et non 403) avec WWW-Authenticate, texte de DRF."""
    assert_401(client.get(PROTECTED), "Informations d'authentification non fournies.")
    assert django.requests == []


def test_non_bearer_scheme_is_401(client, django):
    """Faille 10."""
    response = client.get(PROTECTED, headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert_401(response, "Informations d'authentification non fournies.")
    assert django.requests == []


@pytest.mark.parametrize("token", ["a" * 5000, "jeton avec espaces", "jéton"])
def test_malformed_token_is_refused_without_calling_django(client, django, token):
    response = client.get(PROTECTED, headers={"Authorization": f"Bearer {token}".encode()})
    assert_401(response, "Jeton invalide ou expiré.")
    assert django.requests == []


# ------------------------------------------------ réponses de Django


def test_valid_token_calls_the_internal_verification_route(client, django):
    response = client.get(PROTECTED, headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert response.json() == {"id": 42, "role": "livreur"}
    assert len(django.requests) == 1
    request = django.requests[0]
    assert str(request.url) == VERIFY_URL
    assert request.method == "GET"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


def test_django_401_is_relayed_with_www_authenticate(client, django):
    """Faille 8 : format commun, message de Django relayé, WWW-Authenticate."""
    django.status_code = 401
    django.json = {"success": False, "status_code": 401, "detail": "Le jeton n'est pas valide.", "errors": {}}
    assert_401(client.get(PROTECTED, headers=AUTH_HEADERS), "Le jeton n'est pas valide.")


def test_django_401_without_detail_uses_default_message(client, django):
    django.status_code = 401
    django.json = ["inattendu"]
    assert_401(client.get(PROTECTED, headers=AUTH_HEADERS), "Jeton invalide ou expiré.")


def test_django_429_is_relayed_with_retry_after(client, django):
    """Faille 11 : 429 avec Retry-After, et non 401 (qui ferait rafraîchir
    le jeton, voire déconnecter l'utilisateur, à tort)."""
    django.status_code = 429
    django.headers = {"Retry-After": "120"}
    django.json = {"detail": "Requête ralentie. Disponible à nouveau dans 120 secondes."}
    response = client.get(PROTECTED, headers=AUTH_HEADERS)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "120"
    assert response.json()["detail"] == "Requête ralentie. Disponible à nouveau dans 120 secondes."
    assert response.json()["errors"] == {}


@pytest.mark.parametrize("status_code", [500, 502, 503, 404, 400, 403, 301])
def test_django_errors_and_unexpected_statuses_are_503(client, django, status_code):
    """Faille 12-13 : une panne ou une mauvaise configuration de Django
    n'est pas un jeton invalide."""
    django.status_code = status_code
    response = client.get(PROTECTED, headers=AUTH_HEADERS)
    assert response.status_code == 503
    assert response.json() == {
        "success": False,
        "status_code": 503,
        "detail": "Service d'authentification indisponible.",
        "errors": {},
    }


@pytest.mark.parametrize(
    "error",
    [httpx.ReadTimeout("timeout"), httpx.ConnectTimeout("timeout"), httpx.ConnectError("refused")],
)
def test_django_unreachable_is_503_common_format(client, django, error):
    """Faille 17 : 503 au format commun."""
    django.error = error
    response = client.get(PROTECTED, headers=AUTH_HEADERS)
    assert response.status_code == 503
    assert response.json()["detail"] == "Service d'authentification indisponible."
    assert response.json()["errors"] == {}


@pytest.mark.parametrize(
    "body",
    [
        {"id": "42", "role": "livreur"},
        {"id": 42, "role": "pirate"},
        {"id": 0, "role": "client"},
        {"id": True, "role": "client"},
        {"role": "client"},
        ["pas", "un", "objet"],
    ],
)
def test_unexpected_django_200_body_is_503(client, django, body, caplog):
    """Contrat avec Django vérifié : id entier, rôle connu."""
    django.json = body
    response = client.get(PROTECTED, headers=AUTH_HEADERS)
    assert response.status_code == 503
    assert "Contrat Django rompu" in caplog.text


# ------------------------------------------------ cache Redis


def test_cache_one_django_call_for_many_requests(client, django, app):
    """Faille 16 : un seul appel à Django pour N requêtes dans la durée du
    cache, avec un seul client httpx partagé (app.state.http)."""
    http_client = app.state.http
    for _ in range(5):
        assert client.get(PROTECTED, headers=AUTH_HEADERS).status_code == 200
    assert len(django.requests) == 1
    assert app.state.http is http_client


def test_cache_key_is_a_sha256_of_the_token_with_ttl(client, redis_server):
    client.get(PROTECTED, headers=AUTH_HEADERS)
    raw = fakeredis.FakeRedis(server=redis_server, decode_responses=True)
    key = "fastapi:auth:v1:" + hashlib.sha256(TOKEN.encode()).hexdigest()
    assert raw.keys("*") == [key]
    assert json.loads(raw.get(key)) == {"id": 42, "role": "livreur"}
    assert 0 < raw.ttl(key) <= 30
    assert TOKEN not in key


@pytest.mark.parametrize("status_code", [401, 429, 503])
def test_refusals_are_not_cached(client, django, redis_server, status_code):
    django.status_code = status_code
    client.get(PROTECTED, headers=AUTH_HEADERS)
    client.get(PROTECTED, headers=AUTH_HEADERS)
    assert len(django.requests) == 2
    assert fakeredis.FakeRedis(server=redis_server).keys("fastapi:auth:*") == []


def test_cache_expiry_calls_django_again(client, django, redis_server):
    client.get(PROTECTED, headers=AUTH_HEADERS)
    fakeredis.FakeRedis(server=redis_server).flushall()  # clé expirée
    client.get(PROTECTED, headers=AUTH_HEADERS)
    assert len(django.requests) == 2


def test_redis_down_falls_back_to_django(client, django, redis_server):
    """Redis indisponible : authentification directe auprès de Django."""
    redis_server.connected = False
    for _ in range(2):
        assert client.get(PROTECTED, headers=AUTH_HEADERS).status_code == 200
    assert len(django.requests) == 2


def test_corrupted_cache_entry_is_ignored(client, django, redis_server):
    key = "fastapi:auth:v1:" + hashlib.sha256(TOKEN.encode()).hexdigest()
    fakeredis.FakeRedis(server=redis_server).set(key, '{"id": 1, "role": "super_admin", "x": ')
    assert client.get(PROTECTED, headers=AUTH_HEADERS).json() == {"id": 42, "role": "livreur"}
    assert len(django.requests) == 1


@pytest.mark.parametrize("settings", [{"auth_cache_ttl": 0}], indirect=True)
def test_cache_can_be_disabled(client, django):
    client.get(PROTECTED, headers=AUTH_HEADERS)
    client.get(PROTECTED, headers=AUTH_HEADERS)
    assert len(django.requests) == 2


# ------------------------------------------------ WebSocket


LIVRAISON_ID = "550e8400-e29b-41d4-a716-446655440000"


def ws_close_code(client, token: str | None = TOKEN) -> int:
    # Module 1 : jeton dans le premier message, plus dans l'URL.
    with client.websocket_connect(f"/livraison/ws/{LIVRAISON_ID}") as websocket:
        if token is not None:
            websocket.send_json({"type": "auth", "token": token})
        with pytest.raises(WebSocketDisconnect) as error:
            websocket.receive_text()
    return error.value.code


@pytest.mark.parametrize("settings", [{"ws_auth_timeout": 0.2}], indirect=True)
def test_websocket_without_token_closes_4401(client):
    assert ws_close_code(client, None) == 4401


@pytest.mark.parametrize("status_code, close_code", [(401, 4401), (429, 4429), (503, 1011), (500, 1011)])
def test_websocket_close_codes_follow_django_answer(client, django, status_code, close_code):
    django.status_code = status_code
    assert ws_close_code(client) == close_code


def test_websocket_valid_token_is_accepted(client, django, db):
    # Jeton accepté : un seul appel à Django, puis contrôle des droits
    # (module 1). Livraison inconnue ici : 4403, et non 4401.
    assert ws_close_code(client) == 4403
    assert len(django.requests) == 1
    assert db.queries
