import time
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from app.core.config import settings
from app.routeurs import recherche, conseiller_ia, scan_qr, suivi_temps_reel

# Configuration du logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("anitche.fastapi")

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Microservice haute performance ANITCHE : Recherche plein texte & facettes, Conseiller Shopping IA, Vérification passeports QR et Suivi GPS temps réel (WebSockets).",
    docs_url="/docs",
    redoc_url="/redoc",
)


# 1. Middleware de Sécurité (Headers HTTP)
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


# 2. Middleware de Télémétrie & Latence
class ProcessTimeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        response = await call_next(request)
        process_time = time.time() - start_time
        response.headers["X-Process-Time-Ms"] = str(round(process_time * 1000, 2))
        return response


# Ajout des Middlewares
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(ProcessTimeMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 3. Gestionnaire global d'exceptions
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Exception non gérée sur {request.method} {request.url.path}: {str(exc)}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "status_code": 500,
            "detail": "Une erreur interne est survenue sur le service rapide ANITCHE.",
        },
    )


# Inclusion des routeurs
app.include_router(recherche.router)
app.include_router(conseiller_ia.router)
app.include_router(scan_qr.router)
app.include_router(suivi_temps_reel.router)


@app.get("/health", tags=["Système"])
async def health_check():
    """Healthcheck endpoint pour le monitoring et orchestration Docker."""
    return {
        "status": "healthy",
        "app": settings.app_name,
        "version": settings.app_version,
    }


@app.get("/", tags=["Système"])
async def root():
    """Page d'accueil de l'API FastAPI."""
    return {
        "message": "Bienvenue sur l'API FastAPI d'ANITCHE",
        "documentation": "/docs",
        "endpoints": {
            "recherche": "/recherche/produits",
            "suggestions": "/recherche/suggestions",
            "ia_conseil": "/ia/conseil",
            "qr_scan": "/qr/scan",
            "suivi_gps": "/livraison/position/{livraison_id}",
            "websocket_suivi": "/livraison/ws/{livraison_id}",
        },
    }