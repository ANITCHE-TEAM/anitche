"""Cache Redis de valeurs calculées (JSON), avec durée de vie.

Clés `fastapi:cache:<espace>:v1:<sha256 des paramètres>` dans la base Redis
de FastAPI (2). Les paramètres ne sont jamais écrits en clair dans la clé.

Facultatif, comme le cache d'authentification (app/core/auth.py) : Redis
indisponible en lecture ou en écriture → la valeur est calculée sans cache
et l'incident journalisé. Les limites de débit, elles, refusent (503).
"""
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from redis.exceptions import RedisError

logger = logging.getLogger("anitche.fastapi.cache")

KEY_PREFIX = "fastapi:cache:"
# À incrémenter si la forme d'une valeur en cache change : les anciennes
# clés sont alors ignorées (et expirent seules).
KEY_VERSION = "v1"


def cache_key(namespace: str, parameters: dict[str, Any]) -> str:
    canonical = json.dumps(parameters, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"{KEY_PREFIX}{namespace}:{KEY_VERSION}:{digest}"


async def cache_get(redis, key: str) -> Any | None:
    try:
        cached = await redis.get(key)
    except (RedisError, OSError) as exc:
        logger.warning("Cache indisponible (lecture) : %s", type(exc).__name__)
        return None
    if cached is None:
        return None
    try:
        return json.loads(cached)
    except ValueError:
        return None


async def cache_set(redis, key: str, value: Any, ttl: int) -> None:
    try:
        await redis.set(key, json.dumps(value, ensure_ascii=False, separators=(",", ":")), ex=ttl)
    except (RedisError, OSError) as exc:
        logger.warning("Cache indisponible (écriture) : %s", type(exc).__name__)


async def cached(redis, key: str, ttl: int, compute: Callable[[], Awaitable[Any]]) -> Any:
    """Valeur en cache, sinon `compute()` puis mise en cache pour `ttl`
    secondes. ttl = 0 : toujours calculée, jamais mise en cache."""
    if ttl <= 0:
        return await compute()
    value = await cache_get(redis, key)
    if value is not None:
        return value
    value = await compute()
    await cache_set(redis, key, value, ttl)
    return value
