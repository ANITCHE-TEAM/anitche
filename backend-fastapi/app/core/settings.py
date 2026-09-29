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
    # Conseiller IA (docs/MODULE_IA.md), par utilisateur authentifié.
    "ai_advice": "20/hour",
    "ai_recommendations": "120/hour",
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
    # Même variable et même valeur que Django : base de l'URL imprimée dans
    # les QR des passeports. Scan QR (docs/MODULE_SCAN_QR.md) : seule
    # origine acceptée au décodage, base de url_verification_publique.
    frontend_base_url: str = "http://localhost:5173"
    # Recherche (docs/MODULE_RECHERCHE.md). Adresse publique de ce service,
    # préfixe nginx compris (« https://anitche.com/fast » en prod) : liens
    # next/previous. Jamais déduite de l'en-tête Host (forgeable, et uvicorn
    # tourne avec --no-proxy-headers : le schéma lu serait http).
    public_base_url: str = "http://localhost:8001"
    # Adresse publique des fichiers média de Django (MEDIA_URL servi) : la
    # vue du catalogue ne donne que le chemin relatif des images.
    media_base_url: str = "http://localhost:8000/media/"
    # Durée du cache Redis des totaux, facettes et suggestions (secondes).
    # 0 : pas de cache. Jamais de cache sur la page de résultats.
    search_cache_ttl: int = Field(default=60, ge=0, le=300)
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

    # Taille maximale du corps d'une requête (octets), toutes routes : 413
    # au-delà (app/core/body_limit.py). Le plus gros corps légitime est celui
    # du conseiller IA (10 messages de 1 000 caractères, jusqu'à 12 octets
    # par caractère une fois échappés en JSON).
    max_request_body_bytes: int = Field(default=131_072, ge=1024, le=1_048_576)

    # Conseiller IA (docs/MODULE_IA.md). AI_PROVIDER : clé du registre
    # app/services/conseiller/fournisseurs/__init__.py, vérifiée au
    # démarrage (create_app). « simule » : aucun appel réseau, en dev comme
    # en prod. AI_ENABLED=false : 503 conseiller_desactive.
    ai_enabled: bool = True
    ai_provider: str = Field(default="simule", min_length=1, max_length=40)
    # Délai maximal d'un appel au fournisseur (secondes) ; au-delà, repli sur
    # le fournisseur simulé.
    ai_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    # Appels à un fournisseur PAYANT par jour (UTC), tous utilisateurs ; au-delà,
    # repli sur le simulé. 0 : aucun appel payant. Sans effet sur le simulé.
    ai_daily_call_limit: int = Field(default=1000, ge=0, le=1_000_000)

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

    # Formes uniques, pour construire les liens par simple concaténation :
    # PUBLIC_BASE_URL sans « / » final (suivi du chemin de la route),
    # MEDIA_BASE_URL avec (suivi du chemin relatif de l'image).
    @field_validator("public_base_url")
    @classmethod
    def _strip_public_base_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("media_base_url")
    @classmethod
    def _slash_media_base_url(cls, value: str) -> str:
        return value.rstrip("/") + "/"

    @model_validator(mode="after")
    def _check_urls(self):
        schemes = {
            "DJANGO_API_BASE_URL": (self.django_api_base_url, {"http", "https"}),
            "DATABASE_URL": (self.database_url, {"postgresql", "postgres"}),
            "REDIS_URL": (self.redis_url, {"redis", "rediss"}),
            "FRONTEND_BASE_URL": (self.frontend_base_url, {"http", "https"}),
            "PUBLIC_BASE_URL": (self.public_base_url, {"http", "https"}),
            "MEDIA_BASE_URL": (self.media_base_url, {"http", "https"}),
        }
        for name, (url, allowed) in schemes.items():
            parts = urlsplit(url)
            if parts.scheme not in allowed or not parts.hostname:
                raise ValueError(f"{name} invalide (schéma attendu : {', '.join(sorted(allowed))})")
        # Adresses publiques recopiées dans les réponses : ni paramètres ni
        # fragment, qui se retrouveraient au milieu des liens construits.
        public_urls = (
            ("FRONTEND_BASE_URL", self.frontend_base_url),
            ("PUBLIC_BASE_URL", self.public_base_url),
            ("MEDIA_BASE_URL", self.media_base_url),
        )
        for name, url in public_urls:
            parts = urlsplit(url)
            if parts.query or parts.fragment or "@" in parts.netloc:
                raise ValueError(f"{name} invalide (ni paramètres, ni fragment, ni identifiants)")
        # Origine lue à chaque décodage QR : un port invalide y lèverait une
        # erreur (500) au lieu d'un refus au démarrage.
        try:
            urlsplit(self.frontend_base_url).port
        except ValueError:
            raise ValueError("FRONTEND_BASE_URL invalide (port)") from None
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

        if not self.public_base_url.startswith("https://") or _host(self.public_base_url) in LOCAL_HOSTS:
            problems.append("PUBLIC_BASE_URL doit être l'adresse https:// publique de FastAPI (ex. https://anitche.com/fast)")

        if not self.media_base_url.startswith("https://") or _host(self.media_base_url) in LOCAL_HOSTS:
            problems.append("MEDIA_BASE_URL doit être l'adresse https:// publique des fichiers média de Django")

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
