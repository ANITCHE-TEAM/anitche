"""Adresse IP du client (app/core/network.py) et /health."""
import asyncio
from types import SimpleNamespace

import fakeredis
import pytest
from starlette.requests import HTTPConnection

from app import main as main_module
from app.core.network import client_ip


def connection(trusted_proxy_count: int, forwarded_for: list[str] | None = None, remote="10.0.0.1", kind="http"):
    app = SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(trusted_proxy_count=trusted_proxy_count)))
    headers = [(b"x-forwarded-for", value.encode()) for value in forwarded_for or []]
    scope = {"type": kind, "headers": headers, "client": (remote, 1234) if remote else None, "app": app}
    return HTTPConnection(scope)


@pytest.mark.parametrize(
    "count, forwarded_for, remote, expected",
    [
        # Aucun proxy de confiance : X-Forwarded-For ignoré (falsifiable).
        (0, ["203.0.113.7"], "10.0.0.1", "10.0.0.1"),
        # nginx écrase X-Forwarded-For avec l'IP réelle : dernière entrée.
        (1, ["203.0.113.7"], "172.18.0.5", "203.0.113.7"),
        # Première entrée ajoutée par le client : ignorée.
        (1, ["6.6.6.6, 203.0.113.7"], "172.18.0.5", "203.0.113.7"),
        # Plusieurs en-têtes : joints, comme un serveur WSGI.
        (1, ["6.6.6.6", "203.0.113.7"], "172.18.0.5", "203.0.113.7"),
        (2, ["6.6.6.6, 203.0.113.7, 172.18.0.9"], "172.18.0.5", "203.0.113.7"),
        # Plus de proxys déclarés que d'entrées : la première.
        (3, ["203.0.113.7"], "172.18.0.5", "203.0.113.7"),
        # Proxy déclaré mais pas d'en-tête : adresse TCP.
        (1, None, "172.18.0.5", "172.18.0.5"),
        (1, ["2001:db8::1"], "172.18.0.5", "2001:db8::1"),
        # Adresse invalide ou absente : None (identifiant ip:unknown).
        (1, ["pas-une-ip"], "172.18.0.5", None),
        (1, ["203.0.113.7; DROP TABLE"], "172.18.0.5", None),
        (0, None, "testclient", None),
        (0, None, None, None),
    ],
)
def test_client_ip(count, forwarded_for, remote, expected):
    assert client_ip(connection(count, forwarded_for, remote)) == expected


def test_client_ip_works_for_websockets():
    assert client_ip(connection(1, ["203.0.113.7"], "172.18.0.5", kind="websocket")) == "203.0.113.7"


# ------------------------------------------------------------ /health


def test_health_ok_checks_database_and_redis_without_version(client, db):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}
    assert db.queries == ["SELECT 1"]
    assert "version" not in response.text


def test_health_database_down_is_503(client, db):
    db.error = OSError("connection refused")
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "checks": {"database": "error", "redis": "ok"}}
    assert "connection refused" not in response.text


def test_health_redis_down_is_503(client, redis_server):
    redis_server.connected = False
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "checks": {"database": "ok", "redis": "error"}}


def test_health_slow_database_times_out(client, db, monkeypatch):
    monkeypatch.setattr(main_module, "HEALTH_CHECK_TIMEOUT", 0.05)

    async def slow(query):
        await asyncio.sleep(1)

    db.fetchval = slow
    assert client.get("/health").json()["checks"]["database"] == "error"


def test_health_is_not_rate_limited(client, redis_server):
    for _ in range(3):
        client.get("/health")
    assert fakeredis.FakeRedis(server=redis_server).keys("fastapi:rl:*") == []
