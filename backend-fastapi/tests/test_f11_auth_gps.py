"""F-11 (security audit): access-denied cases on the GPS tracking routes.

Referenced from test_api.py::test_mise_a_jour_position_gps_et_consultation
but never created — this file covers the missing rejection paths:
missing token, unauthorized role, impersonation of another driver, and
explicit refusal by verifier_acces_livraison (role scoping on the Django side).
"""
import pytest
from fastapi import status
from starlette.websockets import WebSocketDisconnect

from app.core.auth import CurrentUser, get_current_user

# Module 0 (socle) : plus d'application globale ni de app.core.securite.
# Chaque test reçoit la fixture `client` (tests/conftest.py), et la
# dépendance simulée est get_current_user, qui renvoie un CurrentUser.

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


def test_position_refuse_sans_authentification(client):
    """No Authorization header: HTTPBearer must reject before reaching the view."""
    response = client.post("/livraison/position", json=PAYLOAD)
    # Module 0 : 403 -> 401 volontaire. Sans authentification, la réponse
    # est 401 (avec WWW-Authenticate), pour que le frontend rafraîchisse
    # le jeton ; 403 est réservé à « authentifié mais non autorisé ».
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_consultation_position_refuse_sans_authentification(client):
    response = client.get(f"/livraison/position/{LIVRAISON_ID}")
    # Module 0 : 403 -> 401 volontaire (voir le test précédent).
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_position_refuse_role_non_livreur(client):
    """An authenticated client cannot publish a GPS position."""
    client.app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=42, role="client")
    response = client.post("/livraison/position", json=PAYLOAD)
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_position_refuse_usurpation_autre_livreur(client):
    """An authenticated driver (id=99) cannot publish for livreur_id=42."""
    client.app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=99, role="livreur")
    response = client.post("/livraison/position", json=PAYLOAD)
    assert response.status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.skip(
    reason="Module 0 : verifier_acces_livraison est retirée du socle ; la règle "
    "d'accès à la livraison sera refaite et testée au module 1 (PostgreSQL)."
)
def test_position_refuse_si_livraison_non_accessible(client):
    """verifier_acces_livraison (Django scoping) refuses -> must propagate."""


def test_websocket_ferme_sans_token(client):
    """
    The server closes the handshake before accepting it. Starlette's
    TestClient raises WebSocketDisconnect as soon as the connection opens,
    so the close code is checked on the exception.
    """
    with pytest.raises(WebSocketDisconnect) as erreur:
        with client.websocket_connect(f"/livraison/ws/{LIVRAISON_ID}"):
            pass

    assert erreur.value.code == CODE_FERMETURE_NON_AUTHENTIFIE


def test_websocket_ferme_token_invalide(client, django):
    # Module 0 : verifier_jwt_brut n'existe plus. Le refus vient désormais
    # du Django simulé (tests/conftest.py), qui répond 401 à la vérification.
    django.status_code = 401
    django.json = {"detail": "Le jeton n'est pas valide."}
    with pytest.raises(WebSocketDisconnect) as erreur:
        with client.websocket_connect(f"/livraison/ws/{LIVRAISON_ID}?token=invalide"):
            pass

    assert erreur.value.code == CODE_FERMETURE_NON_AUTHENTIFIE