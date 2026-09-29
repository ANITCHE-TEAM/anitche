"""Fabrique de l'application FastAPI.

Lancement : `uvicorn --factory app.main:create_app` (voir le Dockerfile).
Aucune application n'est créée à l'import : chaque test construit la
sienne avec ses réglages et ses ressources.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.access_log import AccessLogMiddleware, redact_websocket_query_strings
from app.core.body_limit import BodyLimitMiddleware
from app.core.errors import ERROR_RESPONSES, install_error_handlers
from app.core.resources import Resources, build_lifespan
from app.core.settings import Settings, get_settings
from app.routeurs import conseiller_ia, recherche, scan_qr, suivi_temps_reel
from app.services.conseiller.fournisseurs import creer_fournisseur
from app.services.conseiller.fournisseurs.base import FournisseurIA
from app.services.tracking import TrackingHub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("anitche.fastapi")

HEALTH_CHECK_TIMEOUT = 1.0


async def _check(name: str, probe) -> str:
    try:
        async with asyncio.timeout(HEALTH_CHECK_TIMEOUT):
            await probe()
    except Exception as exc:  # toute panne rend le service non prêt
        logger.warning("Healthcheck %s en échec : %s", name, type(exc).__name__)
        return "error"
    return "ok"


async def health(request: Request) -> JSONResponse:
    """État de PostgreSQL et de Redis, sans version ni détail technique.
    200 si tout répond, 503 sinon (healthcheck Docker, nginx)."""
    state = request.app.state
    checks = {
        "database": await _check("database", lambda: state.db.fetchval("SELECT 1")),
        "redis": await _check("redis", state.redis.ping),
    }
    healthy = all(value == "ok" for value in checks.values())
    return JSONResponse(
        {"status": "ok" if healthy else "error", "checks": checks},
        status_code=200 if healthy else 503,
    )


def build_app_lifespan(settings: Settings, resources: Resources | None, ai_provider: FournisseurIA | None):
    """Ressources partagées (app/core/resources.py), puis l'abonnement
    pub/sub du suivi GPS : un seul par processus, arrêté avant les
    ressources. `ai_provider` : fournisseur du conseiller créé par
    create_app, fermé à l'arrêt (None s'il a été injecté par un test)."""
    resources_lifespan = build_lifespan(settings, resources)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with resources_lifespan(app):
            hub = TrackingHub(app.state.redis, queue_size=settings.tracking_queue_size)
            await hub.start()
            app.state.tracking_hub = hub
            try:
                yield
            finally:
                await hub.stop()
                if ai_provider is not None:
                    await ai_provider.fermer()

    return lifespan


def create_app(
    settings: Settings | None = None,
    *,
    resources: Resources | None = None,
    ai_provider: FournisseurIA | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    # Fournisseur du conseiller IA (AI_PROVIDER) : une valeur inconnue
    # empêche le démarrage, avec la liste des valeurs possibles.
    owned_ai_provider = None if ai_provider is not None else creer_fournisseur(settings)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Microservice haute performance ANITCHE : recherche, conseiller shopping IA, "
            "décodage des QR des passeports (certifiés par Django) et suivi GPS temps réel (WebSockets)."
        ),
        debug=settings.debug,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        root_path=settings.root_path,
        responses=ERROR_RESPONSES,
        lifespan=build_app_lifespan(settings, resources, owned_ai_provider),
    )
    app.state.settings = settings
    app.state.conseiller_ia = ai_provider or owned_ai_provider

    install_error_handlers(app)
    redact_websocket_query_strings()

    # Taille maximale des corps (413), À L'INTÉRIEUR de CORS : le refus
    # porte les en-têtes CORS (le dernier middleware ajouté est le plus
    # externe).
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    # Authentification par en-tête Authorization, sans cookie : pas de
    # credentials cross-origin (comme CORS_ALLOW_CREDENTIALS = False côté
    # Django). Retry-After exposé pour que le frontend le lise en dev.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["Retry-After"],
        max_age=600,
    )
    app.add_middleware(AccessLogMiddleware)

    app.include_router(recherche.router)
    app.include_router(conseiller_ia.router)
    app.include_router(scan_qr.router)
    app.include_router(suivi_temps_reel.router)

    app.add_api_route("/health", health, methods=["GET"], tags=["Système"], summary="État du service")

    @app.get("/", tags=["Système"])
    async def root():
        """Page d'accueil de l'API FastAPI."""
        content = {
            "message": "Bienvenue sur l'API FastAPI d'ANITCHE",
            "endpoints": {
                "recherche": "/recherche/produits",
                "suggestions": "/recherche/suggestions",
                "ia_conseil": "/ia/conseil",
                "ia_recommandations": "/ia/recommandations",
                "qr_scan": "/qr/scan",
                "suivi_gps": "/livraison/position/{livraison_id}",
                "websocket_suivi": "/livraison/ws/{livraison_id}",
            },
        }
        if settings.docs_enabled:
            content["documentation"] = "/docs"
        return content

    return app
