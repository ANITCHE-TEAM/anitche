"""Doubles de test partagés : Django simulé, pool PostgreSQL simulé,
réglages de test."""
import httpx

from app.core.settings import Settings

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjo0Mn0.c2lnbmF0dXJl"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}
DJANGO_BASE_URL = "http://django.test/api"
VERIFY_URL = f"{DJANGO_BASE_URL}/utilisateurs/jeton/verification/"


class FakeDjango:
    """Django simulé. Par défaut, le jeton est valide (livreur 42)."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status_code = 200
        self.json = {"id": 42, "role": "livreur"}
        self.headers: dict[str, str] = {}
        self.error: Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status_code, json=self.json, headers=self.headers)


class FakePool:
    """Pool asyncpg simulé : seul fetchval("SELECT 1") est utilisé (/health)."""

    def __init__(self):
        self.error: Exception | None = None
        self.queries: list[str] = []

    async def fetchval(self, query: str):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return 1

    async def close(self):
        pass


def make_settings(**overrides) -> Settings:
    values = {"environment": "test", "django_api_base_url": DJANGO_BASE_URL, **overrides}
    return Settings(_env_file=None, **values)
