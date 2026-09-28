"""F-11 (security audit): access-denied cases on the GPS tracking routes.

Referenced from test_api.py::test_mise_a_jour_position_gps_et_consultation
but never created — this file covers the missing rejection paths:
missing token, unauthorized role, impersonation of another driver, and
explicit refusal by verifier_acces_livraison (role scoping on the Django side).

Module 1 : l'accès à la livraison est lu dans PostgreSQL (FakePool), le
WebSocket s'authentifie par son premier message (plus de ?token=). Tous
les cas du module : test_suivi_gps.py.
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


@pytest.fixture(autouse=True)
def livraison_en_cours(db):
    """Livraison en cours assignée au livreur 42 (client 7)."""
    db.add_delivery(LIVRAISON_ID, status="en_cours", courier_id=42, client_id=7)
    db.add_user(42, "livreur")
    db.add_user(99, "livreur")
    db.add_user(8, "client")


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
    assert response.json()["errors"] == {"code": ["acces_reserve_livreurs"]}


def test_position_refuse_usurpation_autre_livreur(client, redis):
    """An authenticated driver (id=99) cannot publish for the delivery of
    driver 42. Module 1 : livreur_id du corps ignoré ; le refus vient du
    livreur assigné, lu en base (403 livraison_non_assignee)."""
    client.app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=99, role="livreur")
    response = client.post("/livraison/position", json=PAYLOAD)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["errors"] == {"code": ["livraison_non_assignee"]}


def test_position_refuse_si_livraison_non_accessible(client):
    """Module 1 (réactivé) : la règle d'accès de Django est appliquée.
    Un client sans lien avec la livraison ne lit pas la position (404,
    sans révéler l'existence), un livreur non assigné ne la publie pas."""
    client.app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=42, role="livreur")
    assert client.post("/livraison/position", json=PAYLOAD).status_code == 200

    client.app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=8, role="client")
    response = client.get(f"/livraison/position/{LIVRAISON_ID}")
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["errors"] == {"code": ["livraison_introuvable"]}


@pytest.mark.parametrize("settings", [{"ws_auth_timeout": 0.2}], indirect=True)
def test_websocket_ferme_sans_token(client):
    """
    Module 1 : la connexion est acceptée puis attend le premier message
    {"type": "auth", "token": ...}. Sans lui, fermeture 4401 après le délai
    d'authentification. Starlette's TestClient raises WebSocketDisconnect
    on receive, so the close code is checked on the exception.
    """
    with client.websocket_connect(f"/livraison/ws/{LIVRAISON_ID}") as websocket:
        with pytest.raises(WebSocketDisconnect) as erreur:
            websocket.receive_text()

    assert erreur.value.code == CODE_FERMETURE_NON_AUTHENTIFIE


def test_websocket_ferme_token_invalide(client, django):
    # Module 0 : verifier_jwt_brut n'existe plus. Le refus vient désormais
    # du Django simulé (tests/conftest.py), qui répond 401 à la vérification.
    django.status_code = 401
    django.json = {"detail": "Le jeton n'est pas valide."}
    with client.websocket_connect(f"/livraison/ws/{LIVRAISON_ID}") as websocket:
        websocket.send_json({"type": "auth", "token": "invalide"})
        with pytest.raises(WebSocketDisconnect) as erreur:
            websocket.receive_text()

    assert erreur.value.code == CODE_FERMETURE_NON_AUTHENTIFIE
