"""Journal d'accès : méthode, chemin, statut, durée.

Remplace le journal d'accès d'uvicorn (lancé avec --no-access-log) et
l'ancien en-tête X-Process-Time-Ms. Le chemin est journalisé SANS la chaîne
de requête, qui peut porter un jeton (WebSocket ?token=...).

Middleware ASGI pur : BaseHTTPMiddleware est évité (il copie la réponse
et casse le streaming).
"""
import logging
import time

logger = logging.getLogger("anitche.fastapi.access")


class AccessLogMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            logger.info("%s %s %s %.1fms", scope["method"], scope["path"], status_code, duration_ms)
