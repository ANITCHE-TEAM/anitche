"""F-11 (audit sécurité) : cas d'accès refusé sur les routes de suivi GPS.

Référencé depuis test_api.py::test_mise_a_jour_position_gps_et_consultation
mais jamais créé — ce fichier couvre les chemins de rejet qui manquaient :
absence de jeton, rôle non autorisé, usurpation d'un autre livreur, et
refus explicite de verifier_acces_livraison (scoping par rôle côté Django).
"""
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
from fastapi import HTTPException, status
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


def test_position_refuse_sans_authentification():
    """Aucun header Authorization : HTTPBearer doit rejeter avant d'atteindre la vue."""
    response = client.post("/livraison/position", json=PAYLOAD)
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_consultation_position_refuse_sans_authentification():
    response = client.get(f"/livraison/position/{LIVRAISON_ID}")
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_position_refuse_role_non_livreur():
    """Un client authentifié ne peut pas publier de position GPS."""
    app.dependency_overrides[utilisateur_courant] = lambda: {
        "id": 42, "role": "client", "_token": "faketoken",
    }
    try:
        response = client.post("/livraison/position", json=PAYLOAD)
        assert response.status_code == status.HTTP_403_FORBIDDEN
    finally:
        app.dependency_overrides.clear()


def test_position_refuse_usurpation_autre_livreur():
    """Un livreur authentifié (id=99) ne peut pas publier pour livreur_id=42."""
    app.dependency_overrides[utilisateur_courant] = lambda: {
        "id": 99, "role": "livreur", "_token": "faketoken",
    }
    try:
        response = client.post("/livraison/position", json=PAYLOAD)
        assert response.status_code == status.HTTP_403_FORBIDDEN
    finally:
        app.dependency_overrides.clear()


def test_position_refuse_si_livraison_non_accessible():
    """verifier_acces_livraison (scoping Django) refuse -> doit se propager."""
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
    with client.websocket_connect(f"/livraison/ws/{LIVRAISON_ID}") as ws:
        data = ws.receive()
        assert data.get("type") == "websocket.close"
        assert data.get("code") == 4401


def test_websocket_ferme_token_invalide():
    with patch(
        "app.routeurs.suivi_temps_reel.verifier_jwt_brut",
        new=AsyncMock(side_effect=HTTPException(status.HTTP_401_UNAUTHORIZED, "Jeton invalide ou expiré.")),
    ):
        with client.websocket_connect(f"/livraison/ws/{LIVRAISON_ID}?token=invalide") as ws:
            data = ws.receive()
            assert data.get("type") == "websocket.close"
            assert data.get("code") == 4401
