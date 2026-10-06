"""Limites de débit partagées par toutes les instances FastAPI (Redis).

Fenêtre fixe : un compteur par scope, identifiant et fenêtre
(`fastapi:rl:<scope>:<ident>:<début de fenêtre>`), incrémenté et daté dans
une transaction Redis. Débits au format DRF dans RATE_LIMITS.

Identifiant : `user:<id>` pour une route authentifiée, `ip:<adresse>`
sinon (comme UserRateThrottle et AnonRateThrottle de DRF).

Redis indisponible : 503. Laisser passer sans limite ouvrirait les routes
coûteuses (IA) aux abus.
"""
import logging
import math
import time
from typing import Literal

from fastapi import Depends, HTTPException, Request
from redis.exceptions import RedisError
from starlette.requests import HTTPConnection

from app.core.auth import CurrentUser, get_current_user
from app.core.errors import SERVICE_UNAVAILABLE_MESSAGE
from app.core.network import client_ip
from app.core.settings import DEFAULT_RATE_LIMITS, parse_rate

logger = logging.getLogger("anitche.fastapi.rate_limit")

KEY_PREFIX = "fastapi:rl:"


def _throttled_message(retry_after: int) -> str:
    # Même texte que DRF (Throttled) en français.
    unit = "seconde" if retry_after == 1 else "secondes"
    return f"Requête ralentie. Disponible à nouveau dans {retry_after} {unit}."


async def consume(connection: HTTPConnection, scope: str, ident: str) -> None:
    """Compte une requête. HTTPException 429 (avec Retry-After) au-delà du
    débit du scope, 503 si Redis est indisponible."""
    limit, period = parse_rate(connection.app.state.settings.rate_limits[scope])
    now = time.time()
    window_start = int(now // period) * period
    key = f"{KEY_PREFIX}{scope}:{ident}:{window_start}"

    try:
        async with connection.app.state.redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, period)
            count, _ = await pipe.execute()
    except (RedisError, OSError) as exc:
        logger.error("Limite de débit impossible (Redis indisponible) : %s", type(exc).__name__)
        raise HTTPException(503, SERVICE_UNAVAILABLE_MESSAGE) from None

    if count > limit:
        retry_after = max(1, math.ceil(window_start + period - now))
        raise HTTPException(429, _throttled_message(retry_after), headers={"Retry-After": str(retry_after)})


def rate_limit(scope: str, key: Literal["ip", "user"] = "ip"):
    """Dépendance FastAPI : `dependencies=[Depends(rate_limit("search"))]`.

    Avec key="user", la route est authentifiée (get_current_user, appelée
    une seule fois par requête grâce au cache des dépendances de FastAPI) :
    un client non authentifié reçoit 401 avant d'être compté.
    """
    if scope not in DEFAULT_RATE_LIMITS:
        raise ValueError(f"scope de limite de débit inconnu : {scope}")

    if key == "user":
        async def limit_by_user(request: Request, user: CurrentUser = Depends(get_current_user)) -> None:
            await consume(request, scope, f"user:{user.id}")

        return limit_by_user

    async def limit_by_ip(request: Request) -> None:
        await consume(request, scope, f"ip:{client_ip(request) or 'unknown'}")

    return limit_by_ip
