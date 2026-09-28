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


class RedactWebSocketQueryString(logging.Filter):
    """Retire la chaîne de requête de la ligne de poignée de main WebSocket
    d'uvicorn (« WebSocket /chemin?... [accepted] », journal uvicorn.error),
    écrite même avec --no-access-log. Le suivi GPS n'accepte plus de jeton
    dans l'URL, mais un ancien client qui en enverrait un ne doit pas le
    voir écrit dans les journaux."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and '"WebSocket %s"' in record.msg and isinstance(record.args, tuple):
            record.args = tuple(arg.split("?", 1)[0] if isinstance(arg, str) else arg for arg in record.args)
        return True


def redact_websocket_query_strings() -> None:
    """Installe le filtre une seule fois (create_app peut être appelée
    plusieurs fois dans un processus)."""
    uvicorn_logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(existing, RedactWebSocketQueryString) for existing in uvicorn_logger.filters):
        uvicorn_logger.addFilter(RedactWebSocketQueryString())
