"""Suivi GPS : cas d'accès refusé sur les routes de position et le WebSocket.

Cas refusés de test_api.py::test_mise_a_jour_position_gps_et_consultation :
jeton absent, rôle non autorisé, usurpation d'un autre livreur, refus par
la règle d'accès à la livraison (celle de Django).

L'accès à la livraison est lu dans PostgreSQL (FakePool) ; le WebSocket
s'authentifie par son premier message (aucun ?token= dans l'URL). Tous les
cas du suivi GPS : test_suivi_gps.py.
"""
import pytest
from fastapi import status
from starlette.websockets import WebSocketDisconnect

from app.core.auth import CurrentUser, get_current_user

# Chaque test reçoit la fixture `client` (tests/conftest.py), une
# application neuve ; la dépendance simulée est get_current_user, qui
# renvoie un CurrentUser.

LIVRAISON_ID = "550e8400-e29b-41d4-a716-446655440000"
PAYLOAD = {
    "livraison_id": LIVRAISON_ID,
    "livreur_id": 42,
    "latitude": 5.3350,
    "longitude": -4.0020,
}

# Code de fermeture applicatif envoyé par le serveur quand
# l'authentification du WebSocket est absente ou invalide.
CODE_FERMETURE_NON_AUTHENTIFIE = 4401


@pytest.fixture(autouse=True)
def livraison_en_cours(db):
    """Livraison en cours assignée au livreur 42 (client 7)."""
    db.add_delivery(LIVRAISON_ID, status="en_cours", courier_id=42, client_id=7)
    db.add_user(42, "livreur")
    db.add_user(99, "livreur")
    db.add_user(8, "client")


def test_position_refuse_sans_authentification(client):
    """Sans en-tête Authorization, la requête est refusée avant la vue."""
    response = client.post("/livraison/position", json=PAYLOAD)
    # Sans authentification, la réponse est 401 (avec WWW-Authenticate),
    # et non 403, pour que le frontend rafraîchisse
    # le jeton ; 403 est réservé à « authentifié mais non autorisé ».
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_consultation_position_refuse_sans_authentification(client):
    response = client.get(f"/livraison/position/{LIVRAISON_ID}")
    # 401 et non 403 (voir le test précédent).
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_position_refuse_role_non_livreur(client):
    """Un client authentifié ne publie pas de position GPS."""
    client.app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=42, role="client")
    response = client.post("/livraison/position", json=PAYLOAD)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["errors"] == {"code": ["acces_reserve_livreurs"]}


def test_position_refuse_usurpation_autre_livreur(client, redis):
    """Un livreur authentifié (id=99) ne publie pas pour la livraison du
    livreur 42. Le livreur_id du corps est ignoré ; le refus vient du
    livreur assigné, lu en base (403 livraison_non_assignee)."""
    client.app.dependency_overrides[get_current_user] = lambda: CurrentUser(id=99, role="livreur")
    response = client.post("/livraison/position", json=PAYLOAD)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["errors"] == {"code": ["livraison_non_assignee"]}


def test_position_refuse_si_livraison_non_accessible(client):
    """La règle d'accès de Django est appliquée.
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
    La connexion est acceptée puis attend le premier message
    {"type": "auth", "token": ...}. Sans lui, fermeture 4401 après le délai
    d'authentification. Le TestClient de Starlette lève WebSocketDisconnect
    à la réception : le code de fermeture est lu sur l'exception.
    """
    with client.websocket_connect(f"/livraison/ws/{LIVRAISON_ID}") as websocket:
        with pytest.raises(WebSocketDisconnect) as erreur:
            websocket.receive_text()

    assert erreur.value.code == CODE_FERMETURE_NON_AUTHENTIFIE


def test_websocket_ferme_token_invalide(client, django):
    # Le jeton est vérifié par Django ; le Django simulé (FakeDjango,
    # tests/fakes.py) répond 401 à la vérification.
    django.status_code = 401
    django.json = {"detail": "Le jeton n'est pas valide."}
    with client.websocket_connect(f"/livraison/ws/{LIVRAISON_ID}") as websocket:
        websocket.send_json({"type": "auth", "token": "invalide"})
        with pytest.raises(WebSocketDisconnect) as erreur:
            websocket.receive_text()

    assert erreur.value.code == CODE_FERMETURE_NON_AUTHENTIFIE
