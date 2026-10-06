"""Doubles de test partagés : Django simulé, pool PostgreSQL simulé,
réglages de test."""
from datetime import UTC, datetime
from decimal import Decimal

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
      de app/services/delivery_access.py sur `deliveries` et `users` ;
    - recherche (app/services/search.py) : requête reconnue à sa première
      ligne « -- recherche:<nom> », réponse lue dans `search_rows` (page),
      `search_summary` (total et facettes, calculés depuis `search_rows`
      s'il vaut None), `search_count` (total seul) et `suggestion_rows`.
      Aucun SQL n'est exécuté ici : il l'est sur PostgreSQL dans
      tests/integration/test_recherche_vues.py.

    `queries` : texte de chaque requête ; `calls` : (texte, paramètres).
    """

    def __init__(self):
        self.error: Exception | None = None
        self.queries: list[str] = []
        self.calls: list[tuple[str, tuple]] = []
        self.deliveries: dict[str, dict] = {}
        self.users: dict[int, dict] = {}
        self.search_rows: list[dict] = []
        self.search_summary: dict | None = None
        self.search_count: int | None = None
        self.suggestion_rows: list[dict] = []

    def _record(self, query: str, args: tuple) -> str | None:
        self.queries.append(query)
        self.calls.append((query, args))
        if self.error is not None:
            raise self.error
        first_line = query.lstrip().split("\n", 1)[0]
        return first_line.removeprefix("-- recherche:") if first_line.startswith("-- recherche:") else None

    def search_calls(self, name: str) -> list[tuple[str, tuple]]:
        return [call for call in self.calls if call[0].lstrip().startswith(f"-- recherche:{name}\n")]

    async def fetchval(self, query: str, *args):
        if self._record(query, args) == "total":
            return len(self.search_rows) if self.search_count is None else self.search_count
        return 1

    async def fetch(self, query: str, *args):
        kind = self._record(query, args)
        if kind == "page":
            return list(self.search_rows)
        if kind == "suggestions":
            return list(self.suggestion_rows)
        raise AssertionError(f"requête inattendue : {query[:80]}")

    async def fetchrow(self, query: str, *args):
        kind = self._record(query, args)
        if kind == "resume":
            return self.search_summary if self.search_summary is not None else self._summary_from_rows()
        delivery_id, user_id = args
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

    def _summary_from_rows(self) -> dict:
        prices = [row["prix_min"] for row in self.search_rows]
        return {
            "total": len(self.search_rows),
            "prix_min": min(prices, default=None),
            "prix_max": max(prices, default=None),
            "tranches": "[]",
            "categories": "[]",
            "boutiques": "[]",
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


def product_row(**overrides) -> dict:
    """Ligne de la vue catalogue_produit_public, telle qu'asyncpg la
    renvoie (Decimal, datetime avec fuseau)."""
    row = {
        "id": 41,
        "nom": "Beurre de karité pur",
        "slug": "beurre-de-karite-pur-3d85cf",
        "prix_base": Decimal("3500.00"),
        "prix_min": Decimal("3500.00"),
        "image_principale": "catalogue/produits/2026/09/produit_GJzjrY5.png",
        "miniature_principale": "catalogue/miniatures/2026/09/produit_GJzjrY5.webp",
        "categorie_id": 8,
        "categorie_nom": "Beauté",
        "boutique_id": 14,
        "boutique_nom": "Karité Doré",
        "boutique_slug": "karite-dore",
        "en_stock": True,
        "date_creation": datetime(2026, 9, 27, 20, 27, 3, 31043, tzinfo=UTC),
    }
    return {**row, **overrides}


def make_settings(**overrides) -> Settings:
    values = {"environment": "test", "django_api_base_url": DJANGO_BASE_URL, **overrides}
    return Settings(_env_file=None, **values)
