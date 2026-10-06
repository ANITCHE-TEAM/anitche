"""Format d'erreur commun (identique à Django), CORS, en-têtes et journal
d'accès.

Couvert : CORS, 404, validation (400), 500, 503 au format commun (voir
aussi test_core_auth).
"""
import logging

import pytest
from fastapi.testclient import TestClient

from app.core.errors import INTERNAL_ERROR_MESSAGE
from tests.fakes import AUTH_HEADERS


def assert_common_format(response, status_code: int) -> dict:
    assert response.status_code == status_code
    body = response.json()
    assert set(body) == {"success", "status_code", "detail", "errors"}
    assert body["success"] is False
    assert body["status_code"] == status_code
    assert isinstance(body["detail"], str) and body["detail"]
    assert isinstance(body["errors"], dict)
    return body


def test_unknown_route_404_common_format_in_french(client):
    """Même texte que Django (MESSAGE_RESSOURCE_INTROUVABLE)."""
    body = assert_common_format(client.get("/nexiste-pas"), 404)
    assert body["detail"] == "Ressource introuvable."
    assert body["errors"] == {}


def test_405_same_text_as_django(client):
    response = client.delete("/recherche/produits")
    body = assert_common_format(response, 405)
    assert body["detail"] == "Méthode «\xa0DELETE\xa0» non autorisée."
    assert response.headers["Allow"] == "GET"


def test_query_validation_is_400_with_field_key_and_french_message(client):
    """400 (et non 422), clé du paramètre (sans « query »), message DRF en
    français, detail « champ: message ». Paramètre `recherche`, 3
    caractères au moins pour les suggestions."""
    body = assert_common_format(client.get("/recherche/suggestions?recherche=ab"), 400)
    message = "Assurez-vous que ce champ comporte au moins 3\xa0caractères."
    assert body["errors"] == {"recherche": [message]}
    assert body["detail"] == f"recherche: {message}"


def test_missing_query_parameter_message(client):
    body = assert_common_format(client.get("/recherche/suggestions"), 400)
    assert body["errors"] == {"recherche": ["Ce champ est obligatoire."]}


def test_numeric_bounds_and_parsing_messages(client):
    # Taille de page fixe (20, comme Django, sans paramètre `par_page`) ;
    # la borne est portée par `page` (50 au plus).
    body = assert_common_format(client.get("/recherche/produits?page=500&prix_min=abc"), 400)
    assert body["errors"]["page"] == ["Assurez-vous que cette valeur est inférieure ou égale à 50."]
    assert body["errors"]["prix_min"] == ["Un nombre entier valide est requis."]


def test_nested_body_validation_uses_dotted_keys(client):
    """Clé à points « messages.0.role », comme Django. Route authentifiée (sans jeton, 401 avant la validation)."""
    payload = {"messages": [{"role": "pirate", "contenu": "Bonjour"}]}
    body = assert_common_format(client.post("/ia/conseil", json=payload, headers=AUTH_HEADERS), 400)
    assert body["errors"] == {"messages.0.role": ["«\xa0pirate\xa0» n'est pas un choix valide."]}
    assert body["detail"].startswith("messages.0.role: ")


def test_invalid_json_goes_to_non_field_errors(client):
    response = client.post("/qr/scan", content=b"{pas du json", headers={"Content-Type": "application/json"})
    body = assert_common_format(response, 400)
    assert body["errors"] == {"non_field_errors": ["Le corps de la requête n'est pas un JSON valide."]}
    assert body["detail"] == "Le corps de la requête n'est pas un JSON valide."


def test_missing_body_goes_to_non_field_errors(client):
    body = assert_common_format(client.post("/qr/scan"), 400)
    assert body["errors"] == {"non_field_errors": ["Le corps de la requête est obligatoire."]}


def test_unhandled_exception_500_common_format_without_leak(app, caplog):
    """errors toujours présent, message identique à Django, rien
    de l'exception interne dans la réponse, trace dans les journaux."""

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret interne: mot de passe=hunter2")

    with TestClient(app, raise_server_exceptions=False) as client:
        with caplog.at_level(logging.ERROR, logger="anitche.fastapi.errors"):
            response = client.get("/boom?token=abc")

    body = assert_common_format(response, 500)
    assert body["detail"] == INTERNAL_ERROR_MESSAGE
    assert body["errors"] == {}
    assert "hunter2" not in response.text
    assert "GET /boom" in caplog.text
    assert "token=abc" not in caplog.text


def test_openapi_documents_error_schema_and_no_422(client):
    schema = client.get("/openapi.json").json()
    assert "Error" in schema["components"]["schemas"]
    assert "HTTPValidationError" not in schema["components"]["schemas"]
    responses = schema["paths"]["/recherche/suggestions"]["get"]["responses"]
    assert "422" not in responses
    for code in ("400", "401", "403", "404", "429", "500", "503"):
        assert responses[code]["content"]["application/json"]["schema"]["$ref"].endswith("/Error")


# ---------------------------------------------------------------- CORS


def preflight(client, origin="http://localhost:5173", method="GET", headers="authorization"):
    return client.options(
        "/recherche/produits",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )


def test_cors_preflight_allowed_origin_without_credentials(client):
    """Aucun Access-Control-Allow-Credentials : l'API s'authentifie par
    en-tête Authorization, pas par cookie."""
    response = preflight(client)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-credentials" not in response.headers
    assert response.headers["access-control-max-age"] == "600"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"method": "DELETE"},
        {"headers": "x-anything-goes"},
        {"origin": "http://localhost:3000"},
        {"origin": "https://anitche.ci"},
    ],
)
def test_cors_preflight_refuses_other_methods_headers_and_origins(client, kwargs):
    """Méthodes GET/POST/OPTIONS, en-têtes Authorization et
    Content-Type, origines de CORS_ALLOWED_ORIGINS uniquement."""
    assert preflight(client, **kwargs).status_code == 400


def test_cors_exposes_retry_after(client):
    response = client.get("/", headers={"Origin": "http://localhost:5173"})
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers["access-control-expose-headers"] == "Retry-After"


# ------------------------------------------- en-têtes et journal d'accès


def test_security_and_timing_headers_are_not_set_by_fastapi(client):
    """Les en-têtes de sécurité sont posés par nginx (en double, X-Frame-Options
    deviendrait « DENY, DENY ») ; X-Process-Time-Ms exposerait des durées."""
    response = client.get("/")
    for header in ("X-Frame-Options", "X-XSS-Protection", "X-Content-Type-Options", "X-Process-Time-Ms"):
        assert header not in response.headers


def test_access_log_has_path_status_duration_but_no_query_string(client, caplog):
    with caplog.at_level(logging.INFO, logger="anitche.fastapi.access"):
        client.get("/recherche/suggestions?recherche=wax&token=secret-token")
    records = [record.getMessage() for record in caplog.records if record.name == "anitche.fastapi.access"]
    assert len(records) == 1
    assert records[0].startswith("GET /recherche/suggestions 200 ")
    assert records[0].endswith("ms")
    # Journaux de l'application uniquement (httpx journalise l'URL appelée
    # par le client de test lui-même).
    app_logs = [record.getMessage() for record in caplog.records if record.name.startswith("anitche")]
    assert not any("secret-token" in message for message in app_logs)
