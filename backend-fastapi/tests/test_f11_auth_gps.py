"""F-11 (security audit): access-denied cases on the GPS tracking routes.

Referenced from test_api.py::test_mise_a_jour_position_gps_et_consultation
but never created — this file covers the missing rejection paths:
missing token, unauthorized role, impersonation of another driver, and
explicit refusal by verifier_acces_livraison (role scoping on the Django side).
"""
from unittest.mock import patch, AsyncMock

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.core.securite import utilisateur_courant

client = TestClient(app)

LIVRAISON_ID = "550e8400-e29b-41d4-a716-446655440000"
PAYLOAD = {
    "livraison_id": LIVRAISON_ID,
    "livreur_id": 42,
    "latitude": 5.3350,
    "longitude": -4.0020,
}

# Application-level close code sent by the server when the WebSocket
# handshake is rejected for missing or invalid authentication.
CODE_FERMETURE_NON_AUTHENTIFIE = 4401


def test_position_refuse_sans_authentification():
    """No Authorization header: HTTPBearer must reject before reaching the view."""
    response = client.post("/livraison/position", json=PAYLOAD)
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_consultation_position_refuse_sans_authentification():
    response = client.get(f"/livraison/position/{LIVRAISON_ID}")
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_position_refuse_role_non_livreur():
    """An authenticated client cannot publish a GPS position."""
    app.dependency_overrides[utilisateur_courant] = lambda: {
        "id": 42, "role": "client", "_token": "faketoken",
    }
    try:
        response = client.post("/livraison/position", json=PAYLOAD)
        assert response.status_code == status.HTTP_403_FORBIDDEN
    finally:
        app.dependency_overrides.clear()


def test_position_refuse_usurpation_autre_livreur():
    """An authenticated driver (id=99) cannot publish for livreur_id=42."""
    app.dependency_overrides[utilisateur_courant] = lambda: {
        "id": 99, "role": "livreur", "_token": "faketoken",
    }
    try:
        response = client.post("/livraison/position", json=PAYLOAD)
        assert response.status_code == status.HTTP_403_FORBIDDEN
    finally:
        app.dependency_overrides.clear()


def test_position_refuse_si_livraison_non_accessible():
    """verifier_acces_livraison (Django scoping) refuses -> must propagate."""
    app.dependency_overrides[utilisateur_courant] = lambda: {
        "id": 42, "role": "livreur", "_token": "faketoken",
    }
    try:
        with patch(
            "app.routeurs.suivi_temps_reel.verifier_acces_livraison",
            new=AsyncMock(
                side_effect=HTTPException(status.HTTP_403_FORBIDDEN, "Accès refusé à cette livraison.")
            ),
        ):
            response = client.post("/livraison/position", json=PAYLOAD)
            assert response.status_code == status.HTTP_403_FORBIDDEN
    finally:
        app.dependency_overrides.clear()


def test_websocket_ferme_sans_token():
    """
    The server closes the handshake before accepting it. Starlette's
    TestClient raises WebSocketDisconnect as soon as the connection opens,
    so the close code is checked on the exception.
    """
    with pytest.raises(WebSocketDisconnect) as erreur:
        with client.websocket_connect(f"/livraison/ws/{LIVRAISON_ID}"):
            pass

    assert erreur.value.code == CODE_FERMETURE_NON_AUTHENTIFIE


def test_websocket_ferme_token_invalide():
    with patch(
        "app.routeurs.suivi_temps_reel.verifier_jwt_brut",
        new=AsyncMock(side_effect=HTTPException(status.HTTP_401_UNAUTHORIZED, "Jeton invalide ou expiré.")),
    ):
        with pytest.raises(WebSocketDisconnect) as erreur:
            with client.websocket_connect(f"/livraison/ws/{LIVRAISON_ID}?token=invalide"):
                pass

    assert erreur.value.code == CODE_FERMETURE_NON_AUTHENTIFIE