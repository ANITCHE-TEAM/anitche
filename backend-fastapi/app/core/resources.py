"""Ressources partagées d'un processus : client httpx (Django), pool
PostgreSQL en lecture seule, client Redis.

Créées une seule fois au démarrage (lifespan), rangées dans `app.state`
(`http`, `db`, `redis`) et fermées à l'arrêt. Les tests injectent leurs
propres ressources (create_app(..., resources=...)) : elles ne sont alors ni
créées ni fermées ici.
"""
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx
from fastapi import FastAPI
from redis.asyncio import Redis

from app.core.settings import Settings

logger = logging.getLogger("anitche.fastapi")

DATABASE_SERVER_SETTINGS = {
    "default_transaction_read_only": "on",
    "statement_timeout": "3000",
    "application_name": "anitche-fastapi",
    # asyncpg prépare chaque requête ; après 5 exécutions, PostgreSQL peut
    # garder un plan « générique », établi sans connaître le texte cherché.
    # Pour la recherche, le bon plan dépend du texte (mot rare : index
    # trigramme ; mot courant et tri par date : index de date). Plan
    # recalculé à chaque exécution : environ 1 ms, contre un parcours
    # complet du catalogue dans le mauvais cas (docs/MODULE_RECHERCHE.md).
    "plan_cache_mode": "force_custom_plan",
}


@dataclass
class Resources:
    http: httpx.AsyncClient
    db: Any  # asyncpg.Pool (un double dans les tests)
    redis: Any  # redis.asyncio.Redis (fakeredis dans les tests)


async def open_resources(settings: Settings) -> Resources:
    http = httpx.AsyncClient(
        base_url=settings.django_api_base_url,
        timeout=httpx.Timeout(3.0, connect=1.0),
        limits=httpx.Limits(max_connections=100),
    )
    # redis-py 8 attend 5 s par défaut : trop long pour un cache ou une
    # limite de débit.
    redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=1,
        socket_connect_timeout=1,
        health_check_interval=30,
    )
    try:
        # Échec au démarrage si PostgreSQL est injoignable : le compose
        # redémarre le service. Lecture seule aussi côté connexion, en plus
        # du rôle anitche_fastapi_ro.
        db = await asyncpg.create_pool(
            settings.database_url,
            min_size=1,
            max_size=settings.db_pool_max_size,
            timeout=5,
            server_settings=DATABASE_SERVER_SETTINGS,
        )
    except BaseException:
        await http.aclose()
        await redis.aclose()
        raise
    return Resources(http=http, db=db, redis=redis)


async def close_resources(resources: Resources) -> None:
    await resources.http.aclose()
    await resources.redis.aclose()
    await resources.db.close()


def build_lifespan(settings: Settings, injected: Resources | None):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        resources = injected or await open_resources(settings)
        app.state.http = resources.http
        app.state.db = resources.db
        app.state.redis = resources.redis
        try:
            yield
        finally:
            if injected is None:
                await close_resources(resources)

    return lifespan
