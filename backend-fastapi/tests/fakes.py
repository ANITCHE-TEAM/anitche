"""Doubles de test partagés : Django simulé, pool PostgreSQL simulé,
réglages de test."""
import httpx

from app.core.settings import Settings

TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjo0Mn0.c2lnbmF0dXJl"
AUTH_HEADERS = {"Authorization": f"Bearer {TOKEN}"}
DJANGO_BASE_URL = "http://django.test/api"
VERIFY_URL = f"{DJANGO_BASE_URL}/utilisateurs/jeton/verification/"


class FakeDjango:
    """Django simulé. Par défaut, le jeton est valide (livreur 42).

    `users_by_token` associe un jeton à un utilisateur ({id, role}), ou à
    None pour un jeton révoqué (401). Les autres jetons reçoivent `json`.
    """

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status_code = 200
        self.json = {"id": 42, "role": "livreur"}
        self.headers: dict[str, str] = {}
        self.error: Exception | None = None
        self.users_by_token: dict[str, dict | None] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token in self.users_by_token:
            user = self.users_by_token[token]
            if user is None:
                return httpx.Response(401, json={"detail": "Le jeton n'est pas valide."})
            return httpx.Response(200, json=user)
        return httpx.Response(self.status_code, json=self.json, headers=self.headers)


class FakePool:
    """Pool asyncpg simulé.

    - fetchval("SELECT 1") : /health ;
    - fetchrow(requête d'accès, livraison, utilisateur) : émule la jointure
      de app/services/delivery_access.py sur `deliveries` et `users`.
    """

    def __init__(self):
        self.error: Exception | None = None
        self.queries: list[str] = []
        self.deliveries: dict[str, dict] = {}
        self.users: dict[int, dict] = {}

    async def fetchval(self, query: str):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return 1

    async def fetchrow(self, query: str, delivery_id, user_id):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        delivery = self.deliveries.get(str(delivery_id))
        if delivery is None:
            return None
        user = self.users.get(user_id, {})
        destination = delivery.get("destination") or (None, None)
        return {
            "status": delivery["status"],
            "livreur_id": delivery.get("courier_id"),
            "client_id": delivery["client_id"],
            "role": user.get("role"),
            "is_active": user.get("is_active"),
            "livraison_latitude": destination[0],
            "livraison_longitude": destination[1],
        }

    def add_delivery(self, delivery_id: str, *, status="en_cours", courier_id=42, client_id=7, destination=None):
        self.deliveries[str(delivery_id)] = {
            "status": status,
            "courier_id": courier_id,
            "client_id": client_id,
            "destination": destination,
        }

    def add_user(self, user_id: int, role: str, is_active: bool = True):
        self.users[user_id] = {"role": role, "is_active": is_active}

    async def close(self):
        pass


def make_settings(**overrides) -> Settings:
    values = {"environment": "test", "django_api_base_url": DJANGO_BASE_URL, **overrides}
    return Settings(_env_file=None, **values)
