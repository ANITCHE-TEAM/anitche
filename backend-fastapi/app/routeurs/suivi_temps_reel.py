from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from app.modeles.suivi_temps_reel import PositionGPS, ReponsePositionLivreur
from app.services.websocket_manager import gestionnaire_suivi

router = APIRouter(prefix="/livraison", tags=["Suivi Temps Réel & Télémétrie GPS"])


@router.post(
    "/position",
    response_model=ReponsePositionLivreur,
    summary="Mettre à jour la position GPS du livreur en temps réel",
)
async def mettre_a_jour_position(position: PositionGPS):
    """Reçoit les coordonnées du livreur et les diffuse instantanément aux clients connectés via WebSockets."""
    return await gestionnaire_suivi.diffuser_position(position)


@router.get(
    "/position/{livraison_id}",
    response_model=ReponsePositionLivreur,
    summary="Obtenir la dernière position connue et le temps restant estimé",
)
def obtenir_derniere_position(livraison_id: str):
    """Retourne la position GPS la plus récente ainsi que la distance et le temps d'arrivée estimé."""
    return gestionnaire_suivi.obtenir_derniere_position(livraison_id)


@router.websocket("/ws/{livraison_id}")
async def websocket_suivi_livraison(websocket: WebSocket, livraison_id: str):
    """Canal WebSocket bidirectionnel pour recevoir les coordonnées GPS en streaming continu."""
    await gestionnaire_suivi.connecter(livraison_id, websocket)
    try:
        while True:
            # Maintien de la connexion et écoute d'éventuels messages du client (ping/pong)
            data = await websocket.receive_text()
            # Envoi d'un accusé si nécessaire
            await websocket.send_text(f'{{"type": "ack", "message": "Position reçue: {data}"}}')
    except WebSocketDisconnect:
        gestionnaire_suivi.deconnecter(livraison_id, websocket)
    except Exception:
        gestionnaire_suivi.deconnecter(livraison_id, websocket)