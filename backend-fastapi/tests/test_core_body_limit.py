"""Module 4 : taille maximale du corps des requêtes (app/core/body_limit.py).

Rapport module 3 § 5 et module 4 § 5 : FastAPI lisait et analysait tout
le JSON reçu avant l'authentification (20 Mo acceptés par nginx).
"""
import asyncio
import json

import fakeredis
import pytest

from app.core.body_limit import BodyLimitMiddleware
from app.modeles.conseiller_ia import MAX_CONTENT_LENGTH, MAX_MESSAGES
from tests.fakes import AUTH_HEADERS

LIMIT = 1024
SMALL = {"max_request_body_bytes": LIMIT}
JSON = {"Content-Type": "application/json"}
TOO_LARGE = {
    "success": False, "status_code": 413, "detail": "Le corps de la requête est trop volumineux.",
    "errors": {"code": ["corps_trop_volumineux"]},
}


def qr_body(size: int) -> bytes:
    """Corps JSON de `size` octets exactement pour POST /qr/scan."""
    prefix, suffix = b'{"qr_data": "', b'"}'
    return prefix + b"a" * (size - len(prefix) - len(suffix)) + suffix


@pytest.mark.parametrize("settings", [SMALL], indirect=True)
def test_declared_size_above_limit_is_refused_before_the_app(client, django, db, redis_server):
    body = json.dumps({"messages": [{"role": "user", "contenu": "a" * 2000}]})
    response = client.post("/ia/conseil", content=body, headers={**AUTH_HEADERS, **JSON})
    assert response.status_code == 413 and response.json() == TOO_LARGE
    # Refusé sans lire le corps : ni authentification, ni limite, ni SQL.
    assert django.requests == [] and db.queries == []
    assert fakeredis.FakeRedis(server=redis_server).keys("*") == []


@pytest.mark.parametrize("settings", [SMALL], indirect=True)
def test_limit_is_inclusive(client):
    at_limit = client.post("/qr/scan", content=qr_body(LIMIT), headers=JSON)
    assert at_limit.status_code == 400 and "qr_data" in at_limit.json()["errors"]  # lu et validé par la route
    over = client.post("/qr/scan", content=qr_body(LIMIT + 1), headers=JSON)
    assert over.status_code == 413 and over.json() == TOO_LARGE


@pytest.mark.parametrize("settings", [SMALL], indirect=True)
def test_chunked_body_is_counted_while_read(client):
    chunks = [b'{"qr_data": "', b"a" * 600, b"a" * 600, b'"}']
    response = client.post("/qr/scan", content=iter(chunks), headers=JSON)
    assert response.status_code == 413 and response.json() == TOO_LARGE


@pytest.mark.parametrize("settings", [SMALL], indirect=True)
def test_small_chunked_body_is_delivered_intact(client):
    chunks = [b'{"qr_data": ', b'"PAS-2026-1A2B3C4D"}']
    response = client.post("/qr/scan", content=iter(chunks), headers=JSON)
    assert response.status_code == 200
    assert response.json()["code_passeport"] == "PAS-2026-1A2B3C4D"


@pytest.mark.parametrize("settings", [SMALL], indirect=True)
def test_refusal_carries_cors_headers(client):
    response = client.post(
        "/qr/scan", content=qr_body(LIMIT + 1), headers={**JSON, "Origin": "http://localhost:5173"},
    )
    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


@pytest.mark.parametrize("settings", [SMALL], indirect=True)
def test_requests_without_body_are_untouched(client):
    assert client.get("/").status_code == 200
    assert client.get("/recherche/produits").status_code == 200


def test_default_limit_fits_the_largest_valid_advice(client):
    """10 messages de 1 000 caractères hors du plan multilingue de base,
    échappés en JSON (json.dumps par défaut : 12 octets par caractère)."""
    messages = [{"role": "assistant" if i % 2 else "user", "contenu": "\U0001f600" * MAX_CONTENT_LENGTH}
                for i in range(MAX_MESSAGES)]
    messages[-1]["role"] = "user"
    body = json.dumps({"messages": messages, "occasion": "o" * 60, "style": "s" * 60,
                       "budget_max": 9_999_999_999, "categories": ["a" * 120] * 5})
    assert len(body.encode()) <= client.app.state.settings.max_request_body_bytes
    response = client.post("/ia/conseil", content=body, headers={**AUTH_HEADERS, **JSON})
    assert response.status_code == 200, response.text


def test_malformed_content_length_is_refused():
    called = []

    async def app(scope, receive, send):
        called.append(scope)

    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-length", b"12x")]}
    asyncio.run(BodyLimitMiddleware(app, max_bytes=LIMIT)(scope, receive, send))
    assert called == [] and sent[0]["status"] == 400


def test_other_scopes_pass_through():
    called = []

    async def app(scope, receive, send):
        called.append(scope["type"])

    async def noop(*args):
        return None

    for kind in ("websocket", "lifespan"):
        asyncio.run(BodyLimitMiddleware(app, max_bytes=LIMIT)({"type": kind, "headers": []}, noop, noop))
    assert called == ["websocket", "lifespan"]


def test_openapi_documents_413(client):
    responses = client.get("/openapi.json").json()["paths"]["/qr/scan"]["post"]["responses"]
    assert responses["413"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/Error"}
