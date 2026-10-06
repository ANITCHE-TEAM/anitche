"""Taille maximale du corps des requêtes HTTP (MAX_REQUEST_BODY_BYTES).

Sans elle, FastAPI lit et analyse tout le JSON reçu AVANT l'authentification
et la validation : un client non authentifié faisait analyser jusqu'à
20 Mo (limite de nginx) par requête. Deux contrôles, 413 au format commun
avec `errors.code = ["corps_trop_volumineux"]` :

- Content-Length annoncé au-delà de la limite : refus immédiat, sans lire
  le corps ni appeler l'application ;
- corps sans Content-Length (Transfer-Encoding: chunked) : octets comptés à
  la lecture, exception levée dès le dépassement (jamais plus de la limite
  en mémoire).

Middleware ASGI pur, placé À L'INTÉRIEUR de CORS (app/main.py) : le 413
porte les en-têtes CORS et reste lisible par le frontend. WebSocket et
lifespan : non concernés.
"""
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import DEFAULT_MESSAGES, CodedHTTPException, error_response

BODY_TOO_LARGE_CODE = "corps_trop_volumineux"
BODY_TOO_LARGE_MESSAGE = DEFAULT_MESSAGES[413]
MALFORMED_MESSAGE = DEFAULT_MESSAGES[400]


def _too_large() -> CodedHTTPException:
    return CodedHTTPException(413, BODY_TOO_LARGE_MESSAGE, BODY_TOO_LARGE_CODE)


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = None
        for name, value in scope["headers"]:
            if name == b"content-length":
                declared = value
                break

        if declared is not None:
            # uvicorn refuse déjà un Content-Length mal formé ; ceinture et bretelles.
            if not declared.isdigit():
                await error_response(400, MALFORMED_MESSAGE)(scope, receive, send)
                return
            if int(declared) > self.max_bytes:
                response = error_response(413, BODY_TOO_LARGE_MESSAGE, {"code": [BODY_TOO_LARGE_CODE]})
                await response(scope, receive, send)
                return
            # Le serveur ne livre jamais plus que le Content-Length annoncé.
            await self.app(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # Relevée pendant la lecture du corps par FastAPI : traitée
                    # par le gestionnaire commun (app/core/errors.py), 413.
                    raise _too_large()
            return message

        await self.app(scope, limited_receive, send)
