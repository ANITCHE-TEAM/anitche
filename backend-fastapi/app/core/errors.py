"""Format d'erreur commun aux deux backends.

Identique à Django (config/exceptions.py, GUIDE_FRONTEND.md §5) :

    {"success": false, "status_code": 400, "detail": "q: ...", "errors": {"q": ["..."]}}

- detail : toujours une chaîne, le message principal ;
- errors : toujours un objet, clé -> liste de messages. Clés à points pour
  les champs imbriqués (« messages.0.role »), « non_field_errors » pour une
  erreur qui ne porte sur aucun champ. Vide pour 401, 404, 429...

Les messages reprennent les textes de Django/DRF en français.
"""
import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("anitche.fastapi.errors")

NON_FIELD_ERRORS = "non_field_errors"
GENERIC_ERROR_MESSAGE = "Une erreur est survenue lors du traitement de la requête."
INTERNAL_ERROR_MESSAGE = "Une erreur interne du serveur est survenue. L'incident a été enregistré."
NOT_FOUND_MESSAGE = "Ressource introuvable."
NOT_AUTHENTICATED_MESSAGE = "Informations d'authentification non fournies."
SERVICE_UNAVAILABLE_MESSAGE = "Service temporairement indisponible."

# Message par défaut quand une HTTPException n'en fournit pas (Starlette met
# alors la phrase HTTP anglaise, « Not Found »...). Textes DRF en français.
DEFAULT_MESSAGES = {
    400: "Requête malformée.",
    401: NOT_AUTHENTICATED_MESSAGE,
    403: "Vous n'avez pas la permission d'effectuer cette action.",
    404: NOT_FOUND_MESSAGE,
    413: "Le corps de la requête est trop volumineux.",
    429: "Requête ralentie.",
    503: SERVICE_UNAVAILABLE_MESSAGE,
}

# Codes de fermeture WebSocket (4000-4999 : réservés aux applications).
WS_CLOSE_CODES = {401: 4401, 403: 4403, 429: 4429}
WS_CLOSE_NORMAL = 1000
WS_CLOSE_POLICY_VIOLATION = 1008
WS_CLOSE_MESSAGE_TOO_BIG = 1009
WS_CLOSE_INTERNAL_ERROR = 1011

# Messages de validation Pydantic traduits avec les textes de DRF.
VALIDATION_MESSAGES = {
    "missing": "Ce champ est obligatoire.",
    "string_too_short": "Assurez-vous que ce champ comporte au moins {min_length}\xa0caractères.",
    "string_too_long": "Assurez-vous que ce champ comporte au plus {max_length}\xa0caractères.",
    "greater_than_equal": "Assurez-vous que cette valeur est supérieure ou égale à\xa0{ge}.",
    "less_than_equal": "Assurez-vous que cette valeur est inférieure ou égale à {le}.",
    "less_than": "Assurez-vous que cette valeur est strictement inférieure à {lt}.",
    # NaN, Infinity : refusés (JSON invalide chez les abonnés).
    "finite_number": "Un nombre valide est requis.",
    "int_parsing": "Un nombre entier valide est requis.",
    "int_type": "Un nombre entier valide est requis.",
    "int_from_float": "Un nombre entier valide est requis.",
    "float_parsing": "Un nombre valide est requis.",
    "float_type": "Un nombre valide est requis.",
    "uuid_parsing": "Doit être un UUID valide.",
    "uuid_type": "Doit être un UUID valide.",
    "literal_error": "«\xa0{input}\xa0» n'est pas un choix valide.",
    "bool_parsing": "Doit être un booléen valide.",
    "string_type": "Chaîne de caractère invalide.",
    "list_type": "Attendait une liste d'éléments.",
    "too_short": "Assurez-vous que cette liste comporte au moins {min_length}\xa0élément(s).",
    "too_long": "Assurez-vous que cette liste comporte au plus {max_length}\xa0éléments.",
    # Corps à champs fixes (conseiller IA) : un champ inconnu est refusé,
    # jamais ignoré en silence.
    "extra_forbidden": "Ce champ n'est pas autorisé.",
    "model_attributes_type": "Donnée non valide. Attendait un dictionnaire.",
    "dict_type": "Donnée non valide. Attendait un dictionnaire.",
    "json_invalid": "Le corps de la requête n'est pas un JSON valide.",
}
MISSING_BODY_MESSAGE = "Le corps de la requête est obligatoire."


class Error(BaseModel):
    """Schéma OpenAPI « Error » : toutes les réponses d'erreur."""

    success: bool = False
    status_code: int
    detail: str
    errors: dict[str, list[str]] = {}


ERROR_RESPONSES = {
    code: {"model": Error, "description": HTTPStatus(code).phrase}
    for code in (400, 401, 403, 404, 413, 429, 500, 503)
}


class CodedHTTPException(StarletteHTTPException):
    """HTTPException avec un code machine, renvoyé comme Django dans
    `errors.code = ["<code>"]` : le frontend se fie à `errors.code[0]`,
    jamais au texte français de `detail`."""

    def __init__(self, status_code: int, detail: str, code: str, headers=None):
        super().__init__(status_code, detail, headers)
        self.code = code


def error_response(status_code: int, detail: str, errors: dict | None = None, headers=None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"success": False, "status_code": status_code, "detail": detail, "errors": errors or {}},
        headers=headers,
    )


def _http_detail(request: Request, exc: StarletteHTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, str) and detail and detail != HTTPStatus(exc.status_code).phrase:
        return detail
    if exc.status_code == 405:
        return f"Méthode «\xa0{request.method}\xa0» non autorisée."
    return DEFAULT_MESSAGES.get(exc.status_code, GENERIC_ERROR_MESSAGE)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = getattr(exc, "code", None)
    errors = {"code": [code]} if isinstance(code, str) else None
    return error_response(exc.status_code, _http_detail(request, exc), errors, headers=exc.headers)


def _validation_message(error: dict) -> str:
    template = VALIDATION_MESSAGES.get(error.get("type", ""))
    if template is None:
        return error.get("msg", "Saisie invalide.")
    # Valeur reçue tronquée : elle est renvoyée telle quelle dans le message.
    values = {**error.get("ctx", {}), "input": str(error.get("input"))[:100]}
    try:
        return template.format(**values)
    except (KeyError, IndexError):
        return error.get("msg", "Saisie invalide.")


def _validation_key(error: dict) -> str:
    # loc = ("query", "q"), ("body", "messages", 0, "role")... : la source
    # (query, body, path) est retirée, comme les clés de Django.
    location = error.get("loc", ())
    if error.get("type") == "json_invalid" or len(location) <= 1:
        return NON_FIELD_ERRORS
    return ".".join(str(part) for part in location[1:])


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors: dict[str, list[str]] = {}
    for error in exc.errors():
        if error.get("type") == "missing" and tuple(error.get("loc", ())) == ("body",):
            message = MISSING_BODY_MESSAGE
        else:
            message = _validation_message(error)
        errors.setdefault(_validation_key(error), []).append(message)

    key, messages = next(iter(errors.items()), (NON_FIELD_ERRORS, [GENERIC_ERROR_MESSAGE]))
    detail = messages[0] if key == NON_FIELD_ERRORS else f"{key}: {messages[0]}"
    return error_response(400, detail, errors)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Chemin sans la chaîne de requête : pas de jeton dans les journaux.
    logger.error("Exception non gérée sur %s %s", request.method, request.url.path, exc_info=exc)
    return error_response(500, INTERNAL_ERROR_MESSAGE)


def ws_close_code(status_code: int) -> int:
    return WS_CLOSE_CODES.get(status_code, WS_CLOSE_INTERNAL_ERROR)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # FastAPI documente d'office une réponse 422 (HTTPValidationError) sur
    # les routes à paramètres. La validation répond 400 au format Error :
    # on retire ce 422 du schéma pour ne pas tromper le frontend.
    build_openapi = app.openapi

    def openapi() -> dict:
        if app.openapi_schema is None:
            schema = build_openapi()
            for operations in schema.get("paths", {}).values():
                for operation in operations.values():
                    if isinstance(operation, dict):
                        operation.get("responses", {}).pop("422", None)
            components = schema.get("components", {}).get("schemas", {})
            components.pop("HTTPValidationError", None)
            components.pop("ValidationError", None)
        return app.openapi_schema

    app.openapi = openapi
