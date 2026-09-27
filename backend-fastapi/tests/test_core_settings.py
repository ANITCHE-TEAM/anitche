"""Module 0 : réglages par environnement et validation de la prod.

Failles du rapport module 0 §1 couvertes : 1-3 (CORS), 20-23 (doc, debug,
URL Django), 26 (base Redis partagée avec le cache Django).
"""
import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.resources import Resources
from app.core.settings import DEFAULT_RATE_LIMITS, Settings
from app.main import create_app
from tests.fakes import FakeDjango, FakePool, make_settings

PROD = {
    "environment": "prod",
    "cors_allowed_origins": ["https://anitche.com"],
    "frontend_base_url": "https://anitche.com",
    "django_api_base_url": "http://backend-django:8000/api",
    "database_url": "postgresql://anitche_fastapi_ro:prod-secret@db:5432/anitche",
    "redis_url": "redis://redis:6379/2",
    "trusted_proxy_count": 1,
    "root_path": "/fast",
}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    """Les valeurs par défaut sont testées sans l'environnement du poste ou
    de la CI (ENVIRONMENT=test en CI)."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def prod_settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**PROD, **overrides})


def test_valid_production_settings_are_accepted():
    settings = prod_settings()
    assert settings.environment == "prod"
    assert settings.docs_enabled is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"debug": True},
        {"cors_allowed_origins": []},
        {"cors_allowed_origins": ["*"]},
        {"cors_allowed_origins": ["http://anitche.com"]},
        {"cors_allowed_origins": ["https://localhost:5173"]},
        {"cors_allowed_origins": ["https://anitche.com", "http://127.0.0.1:3000"]},
        {"frontend_base_url": "http://anitche.com"},
        {"frontend_base_url": "https://localhost"},
        {"django_api_base_url": "http://localhost:8000/api"},
        {"django_api_base_url": "http://127.0.0.1:8000/api"},
        {"database_url": "postgresql://anitche_fastapi_ro:prod-secret@localhost:5432/anitche"},
        {"database_url": "postgresql://anitche_fastapi_ro:fastapi_ro_dev@db:5432/anitche"},
        {"database_url": "postgresql://anitche_fastapi_ro:@db:5432/anitche"},
        {"redis_url": "redis://localhost:6379/2"},
        {"trusted_proxy_count": 0},
        {"trusted_proxy_count": 2},
    ],
)
def test_production_refuses_dangerous_settings(overrides):
    """Faille 1-2, 22-23 : la prod refuse de démarrer avec un réglage de dev
    ou dangereux (debug, CORS localhost / * / http, URL Django locale...)."""
    with pytest.raises(ValidationError, match="Configuration de production refusée"):
        prod_settings(**overrides)


def test_defaults_are_safe_for_dev():
    """Faille 22 et 26 : debug faux par défaut ; Redis sur la base 2, jamais
    la base 1 du cache Django ; CORS sans l'ancien domaine anitche.ci."""
    settings = Settings(_env_file=None)
    assert settings.environment == "dev"
    assert settings.debug is False
    assert settings.redis_url.endswith("/2")
    assert settings.cors_allowed_origins == ["http://localhost:5173"]
    assert settings.auth_cache_ttl == 30


def test_cors_origins_are_read_as_csv(monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://anitche.com, https://www.anitche.com")
    assert Settings(_env_file=None).cors_allowed_origins == ["https://anitche.com", "https://www.anitche.com"]


def test_environment_variables_are_read(monkeypatch):
    monkeypatch.setenv("DJANGO_API_BASE_URL", "http://anitche-backend:8000/api")
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "1")
    monkeypatch.setenv("AUTH_CACHE_TTL", "10")
    settings = Settings(_env_file=None)
    assert settings.django_api_base_url == "http://anitche-backend:8000/api"
    assert settings.trusted_proxy_count == 1
    assert settings.auth_cache_ttl == 10


def test_rate_limits_override_is_merged_with_defaults(monkeypatch):
    monkeypatch.setenv("RATE_LIMITS", '{"search": "10/minute"}')
    rates = Settings(_env_file=None).rate_limits
    assert rates["search"] == "10/minute"
    assert rates["ai_advice"] == DEFAULT_RATE_LIMITS["ai_advice"]


@pytest.mark.parametrize("rates", [{"unknown": "1/hour"}, {"search": "10/week"}, {"search": "0/hour"}])
def test_invalid_rate_limits_are_refused(rates):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rate_limits=rates)


@pytest.mark.parametrize(
    "overrides",
    [{"database_url": "mysql://u:p@db/anitche"}, {"redis_url": "http://redis:6379"}, {"root_path": "fast/"}],
)
def test_malformed_urls_are_refused_in_every_environment(overrides):
    with pytest.raises(ValidationError):
        make_settings(**overrides)


def _client_for(settings: Settings) -> TestClient:
    http = httpx.AsyncClient(base_url=settings.django_api_base_url, transport=httpx.MockTransport(FakeDjango()))
    app = create_app(settings, resources=Resources(http=http, db=FakePool(), redis=fakeredis.FakeAsyncRedis()))
    return TestClient(app)


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_documentation_is_disabled_in_production(path):
    """Faille 20-21 : doc coupée en prod par le code, aucune variable ne la
    rouvre. Réponse 404 au format commun."""
    with _client_for(prod_settings()) as client:
        response = client.get(path)
    assert response.status_code == 404
    assert response.json()["success"] is False


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_documentation_is_enabled_in_dev_and_test(path):
    with _client_for(make_settings()) as client:
        assert client.get(path).status_code == 200


def test_debug_setting_is_passed_to_fastapi():
    """Faille 22 : un réglage qui existe doit produire un effet."""
    assert create_app(make_settings(debug=True), resources=None).debug is True
    assert create_app(make_settings(), resources=None).debug is False


def test_create_app_builds_independent_applications():
    """Plus d'application globale créée à l'import : chaque appel à
    create_app renvoie une application neuve, avec ses propres réglages."""
    import app.main as main_module

    assert not hasattr(main_module, "app")
    first = create_app(make_settings(auth_cache_ttl=5))
    second = create_app(make_settings(auth_cache_ttl=10))
    assert first is not second
    assert first.state.settings.auth_cache_ttl == 5
    assert second.state.settings.auth_cache_ttl == 10


def test_startup_error_does_not_leak_secret_values():
    """Une configuration refusée ne recopie pas les valeurs lues (mot de
    passe de DATABASE_URL...) dans le message, écrit dans les journaux."""
    # database_url en premier : sans hide_input_in_errors, pydantic recopie
    # le début des valeurs reçues, donc ce mot de passe, dans l'erreur.
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, database_url="postgresql://a:Secret99@db/x", environment="prod")
    assert "Secret99" not in str(error.value)
