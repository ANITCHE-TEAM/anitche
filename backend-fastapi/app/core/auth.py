"""Authentification déléguée à Django.

FastAPI n'a pas de base d'utilisateurs ni de secret de signature : il
présente le jeton du client à la route interne de Django
`utilisateurs/jeton/verification/`, qui répond {id, role}. La révocation
des jetons et la désactivation des comptes restent gérées par Django.

Cache Redis de AUTH_CACHE_TTL secondes (30 par défaut), clé dérivée du
jeton par sha256 (le jeton n'est jamais stocké en clair). Seules les
réponses 200 sont mises en cache. Compromis assumé : une révocation prend
effet en AUTH_CACHE_TTL secondes au plus.

Correspondance des réponses de Django :
- 200 : utilisateur (corps validé : id entier, rôle connu) ;
- 401 : 401, message de Django relayé, WWW-Authenticate: Bearer ;
- 429 : 429, Retry-After relayé (un 401 ferait rafraîchir le jeton à tort) ;
- 5xx, délai dépassé, erreur réseau, autre statut, corps inattendu : 503.
"""
import hashlib
import logging
import re
from typing import Literal

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.exceptions import RedisError
from starlette.requests import HTTPConnection

from app.core.errors import NOT_AUTHENTICATED_MESSAGE

logger = logging.getLogger("anitche.fastapi.auth")

VERIFY_TOKEN_PATH = "/utilisateurs/jeton/verification/"
CACHE_KEY_PREFIX = "fastapi:auth:v1:"
AUTH_UNAVAILABLE_MESSAGE = "Service d'authentification indisponible."
INVALID_TOKEN_MESSAGE = "Jeton invalide ou expiré."
THROTTLED_MESSAGE = "Requête ralentie."
WWW_AUTHENTICATE = {"WWW-Authenticate": "Bearer"}

# Un JWT : caractères base64url et points. Tout autre jeton est refusé sans
# appeler Django (évite aussi un en-tête non ASCII dans l'appel httpx).
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_\-.=+/~]{1,4096}$")
MAX_RELAYED_DETAIL_LENGTH = 300

bearer_scheme = HTTPBearer(auto_error=False)


class CurrentUser(BaseModel):
    """Utilisateur authentifié, tel que Django le décrit (id et rôle)."""

    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    id: int = Field(gt=0)
    role: Literal["client", "vendeur", "livreur", "moderateur", "support", "admin", "super_admin"]


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(401, detail, headers=WWW_AUTHENTICATE)


def _unavailable() -> HTTPException:
    return HTTPException(503, AUTH_UNAVAILABLE_MESSAGE)


def _django_detail(response: httpx.Response, default: str) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return default
    if isinstance(detail, str) and detail.strip():
        return detail[:MAX_RELAYED_DETAIL_LENGTH]
    return default


async def _verify_with_django(http: httpx.AsyncClient, token: str) -> CurrentUser:
    try:
        response = await http.get(VERIFY_TOKEN_PATH, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        logger.warning("Django injoignable pour la vérification du jeton : %s", type(exc).__name__)
        raise _unavailable() from None

    if response.status_code == 200:
        try:
            return CurrentUser.model_validate_json(response.content)
        except ValidationError:
            logger.error("Contrat Django rompu : réponse 200 inattendue sur %s", VERIFY_TOKEN_PATH)
            raise _unavailable() from None

    if response.status_code == 401:
        raise _unauthorized(_django_detail(response, INVALID_TOKEN_MESSAGE))

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "")
        headers = {"Retry-After": retry_after} if retry_after.isdigit() else None
        raise HTTPException(429, _django_detail(response, THROTTLED_MESSAGE), headers=headers)

    if response.status_code < 500:
        # 404 (route renommée), 400 (DisallowedHost), 301 (redirection HTTPS)... :
        # mauvaise configuration, pas un jeton invalide.
        logger.error("Réponse inattendue de Django sur %s : %s", VERIFY_TOKEN_PATH, response.status_code)
    raise _unavailable()


def _cache_key(token: str) -> str:
    return CACHE_KEY_PREFIX + hashlib.sha256(token.encode()).hexdigest()


async def _cache_get(redis, key: str) -> CurrentUser | None:
    try:
        cached = await redis.get(key)
    except (RedisError, OSError) as exc:
        logger.warning("Cache d'authentification indisponible (lecture) : %s", type(exc).__name__)
        return None
    if cached is None:
        return None
    try:
        return CurrentUser.model_validate_json(cached)
    except ValidationError:
        return None


async def _cache_set(redis, key: str, user: CurrentUser, ttl: int) -> None:
    try:
        await redis.set(key, user.model_dump_json(), ex=ttl)
    except (RedisError, OSError) as exc:
        logger.warning("Cache d'authentification indisponible (écriture) : %s", type(exc).__name__)


async def authenticate_token(connection: HTTPConnection, token: str) -> CurrentUser:
    """Utilisateur correspondant au jeton, ou HTTPException 401/429/503.

    Partagée par les routes HTTP (get_current_user) et les WebSockets, où le
    navigateur ne peut pas poser d'en-tête Authorization.
    """
    if not TOKEN_PATTERN.match(token):
        raise _unauthorized(INVALID_TOKEN_MESSAGE)

    state = connection.app.state
    ttl = state.settings.auth_cache_ttl
    key = _cache_key(token)

    if ttl > 0:
        cached = await _cache_get(state.redis, key)
        if cached is not None:
            return cached

    user = await _verify_with_django(state.http, token)
    if ttl > 0:
        await _cache_set(state.redis, key, user, ttl)
    return user


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> CurrentUser:
    """Dépendance des routes authentifiées. En-tête absent ou autre schéma
    que Bearer : 401 (et non 403), pour que le frontend rafraîchisse le jeton."""
    if credentials is None:
        raise _unauthorized(NOT_AUTHENTICATED_MESSAGE)
    return await authenticate_token(request, credentials.credentials)
