"""Fixtures communes : une application neuve par test, avec des ressources
isolées injectées dans create_app (aucun service réel n'est appelé).

- Django : httpx.MockTransport (tests/fakes.py, FakeDjango), le vrai code de app/core/auth.py
  est exécuté ;
- Redis : fakeredis, un serveur vide par test ;
- PostgreSQL : pool simulé (seul /health l'utilise au module 0).
"""
import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.resources import Resources
from app.core.settings import Settings
from app.main import create_app
from tests.fakes import FakeDjango, FakePool, make_settings


@pytest.fixture
def settings(request) -> Settings:
    # Réglages propres à un test : @pytest.mark.parametrize("settings",
    # [{...}], indirect=True).
    return make_settings(**getattr(request, "param", {}))


@pytest.fixture
def django() -> FakeDjango:
    return FakeDjango()


@pytest.fixture
def redis_server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture
def redis(redis_server):
    return fakeredis.FakeAsyncRedis(server=redis_server, decode_responses=True)


@pytest.fixture
def db() -> FakePool:
    return FakePool()


@pytest.fixture
def app(settings, django, redis, db):
    http = httpx.AsyncClient(base_url=settings.django_api_base_url, transport=httpx.MockTransport(django))
    return create_app(settings, resources=Resources(http=http, db=db, redis=redis))


@pytest.fixture
def client(app):
    # `with` : le lifespan s'exécute (ressources rangées dans app.state).
    with TestClient(app) as test_client:
        yield test_client
