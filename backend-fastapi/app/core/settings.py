"""Réglages du service FastAPI, lus dans l'environnement (et `.env` en dev).

Une seule classe pour les trois environnements (`ENVIRONMENT` : dev, test,
prod). En prod, le validateur refuse de démarrer si un réglage est
dangereux ou pointe encore vers une valeur de dev : même logique que
`config/settings/prod.py` côté Django (ImproperlyConfigured).
"""
import re
from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PERIOD_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
RATE_PATTERN = re.compile(r"^(\d+)/(second|minute|hour|day)$")

# Débits par scope, au format DRF (« N/période »). Chaque route déclare son
# scope (app/core/rate_limit.py). RATE_LIMITS (JSON) remplace tout ou
# partie de ces valeurs.
DEFAULT_RATE_LIMITS = {
    "search": "1200/hour",
    "suggestions": "2400/hour",
    "qr_scan": "600/hour",
    "gps_publish": "1500/hour",
    # Lecture de la position en repli du WebSocket : une toutes les 5 s.
    "gps_read": "720/hour",
    "ws_connect": "60/hour",
    "ai_advice": "20/hour",
}

DEV_DATABASE_PASSWORD = "fastapi_ro_dev"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def parse_rate(rate: str) -> tuple[int, int]:
    """« 20/hour » -> (20, 3600). ValueError si le format est invalide."""
    match = RATE_PATTERN.match(rate.replace(" ", ""))
    if not match or int(match.group(1)) < 1:
        raise ValueError(f"débit invalide « {rate} » (attendu : N/second|minute|hour|day, N >= 1)")
    return int(match.group(1)), PERIOD_SECONDS[match.group(2)]


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class Settings(BaseSettings):
    # hide_input_in_errors : une erreur de démarrage n'écrit pas les valeurs
    # lues (mots de passe de DATABASE_URL, du .env...) dans les journaux.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    environment: Literal["dev", "test", "prod"] = "dev"
    debug: bool = False

    app_name: str = "ANITCHE Fast Engine"
    app_version: str = "1.0.0"
    # Préfixe sous lequel nginx sert l'API (« /fast » en prod), vide en dev.
    root_path: str = ""

    # Même variable et même format (CSV) que Django.
    cors_allowed_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]
    # Même variable que Django : base des liens vers le frontend (QR).
    frontend_base_url: str = "http://localhost:5173"
    # Appels internes à Django (vérification du jeton). Dans Docker (dev) :
    # http://anitche-backend:8000/api, fixé par le compose.
    django_api_base_url: str = "http://localhost:8000/api"

    # Rôle PostgreSQL en lecture seule (infra/postgres/fastapi_readonly.sql).
    database_url: str = f"postgresql://anitche_fastapi_ro:{DEV_DATABASE_PASSWORD}@localhost:5432/anitche"
    db_pool_max_size: int = Field(default=10, ge=1, le=50)
    # Base 2 réservée à FastAPI (0 : Celery, 1 : cache Django).
    redis_url: str = "redis://localhost:6379/2"

    # Nombre de proxys de confiance devant FastAPI (nginx en prod), même
    # rôle que REST_FRAMEWORK['NUM_PROXIES'] côté Django.
    trusted_proxy_count: int = Field(default=0, ge=0, le=5)

    # Durée du cache d'authentification (secondes). 0 : pas de cache.
    auth_cache_ttl: int = Field(default=30, ge=0, le=300)
    rate_limits: dict[str, str] = DEFAULT_RATE_LIMITS

    # Suivi GPS (docs/MODULE_SUIVI_GPS.md).
    # Durée de vie de la dernière position dans Redis (secondes) : au-delà,
    # elle n'est plus montrée (téléphone éteint, tunnel...).
    tracking_position_ttl: int = Field(default=120, ge=10, le=3600)
    # WebSocket : délai du premier message d'authentification, intervalle de
    # revalidation (jeton et droits), taille maximale d'un message reçu
    # (sous --ws-max-size 8192 d'uvicorn), messages du client par minute,
    # positions en attente par connexion (la plus ancienne est jetée).
    ws_auth_timeout: float = Field(default=5.0, gt=0, le=60)
    ws_revalidate_interval: float = Field(default=60.0, gt=0, le=600)
    ws_max_message_bytes: int = Field(default=4096, ge=256, le=8192)
    ws_client_messages_per_minute: int = Field(default=6, ge=1, le=60)
    tracking_queue_size: int = Field(default=8, ge=1, le=100)
    # Estimation INDICATIVE de la distance et du temps restants, seulement si
    # le client a donné son point GPS au checkout. Distance à vol d'oiseau
    # (Haversine) multipliée par un facteur de détour : la route est plus
    # longue que la ligne droite (1,3 à 1,5 en ville, hypothèse à mesurer).
    # Vitesse moyenne urbaine d'une moto ou d'une voiture dans Abidjan,
    # arrêts compris, sans trafic en temps réel (hypothèse à mesurer).
    eta_detour_factor: float = Field(default=1.4, ge=1.0, le=3.0)
    eta_average_speed_kmh: float = Field(default=20.0, gt=0, le=120)

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_csv(cls, value):
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("rate_limits")
    @classmethod
    def _check_rate_limits(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = set(value) - set(DEFAULT_RATE_LIMITS)
        if unknown:
            raise ValueError(f"scopes inconnus dans RATE_LIMITS : {sorted(unknown)}")
        merged = {**DEFAULT_RATE_LIMITS, **value}
        for rate in merged.values():
            parse_rate(rate)
        return merged

    @field_validator("root_path")
    @classmethod
    def _check_root_path(cls, value: str) -> str:
        if value and (not value.startswith("/") or value.endswith("/")):
            raise ValueError("ROOT_PATH doit commencer par « / » et ne pas finir par « / » (ex. /fast)")
        return value

    @model_validator(mode="after")
    def _check_urls(self):
        schemes = {
            "DJANGO_API_BASE_URL": (self.django_api_base_url, {"http", "https"}),
            "DATABASE_URL": (self.database_url, {"postgresql", "postgres"}),
            "REDIS_URL": (self.redis_url, {"redis", "rediss"}),
            "FRONTEND_BASE_URL": (self.frontend_base_url, {"http", "https"}),
        }
        for name, (url, allowed) in schemes.items():
            parts = urlsplit(url)
            if parts.scheme not in allowed or not parts.hostname:
                raise ValueError(f"{name} invalide (schéma attendu : {', '.join(sorted(allowed))})")
        return self

    @model_validator(mode="after")
    def _check_production(self):
        """Refuse de démarrer en prod avec un réglage dangereux."""
        if self.environment != "prod":
            return self

        problems = []
        if self.debug:
            problems.append("DEBUG doit être faux")

        if not self.cors_allowed_origins:
            problems.append("CORS_ALLOWED_ORIGINS doit être défini")
        for origin in self.cors_allowed_origins:
            if "*" in origin or not origin.startswith("https://") or _host(origin) in LOCAL_HOSTS:
                problems.append(f"origine CORS interdite : {origin} (https:// uniquement, ni * ni localhost)")

        if not self.frontend_base_url.startswith("https://") or _host(self.frontend_base_url) in LOCAL_HOSTS:
            problems.append("FRONTEND_BASE_URL doit être l'adresse https:// publique du frontend")

        if _host(self.django_api_base_url) in LOCAL_HOSTS:
            problems.append("DJANGO_API_BASE_URL doit pointer vers le service Django (pas localhost)")

        database = urlsplit(self.database_url)
        if _host(self.database_url) in LOCAL_HOSTS:
            problems.append("DATABASE_URL ne doit pas pointer vers localhost")
        if not database.password or database.password == DEV_DATABASE_PASSWORD:
            problems.append("DATABASE_URL doit contenir le mot de passe de prod du rôle en lecture seule")

        if _host(self.redis_url) in LOCAL_HOSTS:
            problems.append("REDIS_URL ne doit pas pointer vers localhost")

        if self.trusted_proxy_count != 1:
            problems.append("TRUSTED_PROXY_COUNT doit valoir 1 (nginx)")

        if problems:
            raise ValueError("Configuration de production refusée : " + " ; ".join(problems))
        return self

    @property
    def docs_enabled(self) -> bool:
        # Décidé par le code, comme DOCUMENTATION_API_ACTIVE côté Django :
        # aucune variable ne peut rouvrir la doc en prod.
        return self.environment != "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
