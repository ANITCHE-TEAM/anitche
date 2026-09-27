from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from app.modeles.suivi_temps_reel import PositionGPS, ReponsePositionLivreur
from app.services.websocket_manager import gestionnaire_suivi
from app.core.auth import CurrentUser, authenticate_token, get_current_user
from app.core.errors import WS_CLOSE_CODES, ws_close_code
from app.core.rate_limit import consume, rate_limit

router = APIRouter(prefix="/livraison", tags=["Suivi Temps Réel & Télémétrie GPS"])


@router.post(
    "/position",
    response_model=ReponsePositionLivreur,
    summary="Mettre à jour la position GPS du livreur en temps réel",
    dependencies=[Depends(rate_limit("gps_publish", key="user"))],
)
async def mettre_a_jour_position(
    position: PositionGPS,
    utilisateur: CurrentUser = Depends(get_current_user),
):
    """Reçoit les coordonnées du livreur et les diffuse instantanément aux clients connectés via WebSockets.

    F-11 (audit sécurité) : authentification requise, et seul le livreur
    concerné (ou un admin) peut publier une position pour cette livraison —
    sans quoi n'importe qui pourrait injecter de fausses coordonnées GPS
    pour une livraison qui n'est pas la sienne.

    Module 0 : la vérification d'accès à la livraison (livreur assigné) est
    retirée du socle ; elle sera refaite au module 1 (lecture PostgreSQL).
    """
    if utilisateur.role not in ("livreur", "admin", "super_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Accès réservé aux livreurs.")
    if utilisateur.role == "livreur" and utilisateur.id != position.livreur_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Un livreur ne peut publier que sa propre position.",
        )
    return await gestionnaire_suivi.diffuser_position(position)


@router.get(
    "/position/{livraison_id}",
    response_model=ReponsePositionLivreur,
    summary="Obtenir la dernière position connue et le temps restant estimé",
)
async def obtenir_derniere_position(
    livraison_id: str,
    utilisateur: CurrentUser = Depends(get_current_user),
):
    """Retourne la position GPS la plus récente ainsi que la distance et le temps d'arrivée estimé.

    F-11 : authentification requise. Module 0 : la vérification d'accès à
    la livraison (client de la commande, livreur assigné) est retirée du
    socle ; elle sera refaite au module 1 (lecture PostgreSQL). D'ici là,
    tout utilisateur authentifié peut lire une position (module factice).
    """
    return gestionnaire_suivi.obtenir_derniere_position(livraison_id)


@router.websocket("/ws/{livraison_id}")
async def websocket_suivi_livraison(websocket: WebSocket, livraison_id: str, token: str = ""):
    """Canal WebSocket bidirectionnel pour recevoir les coordonnées GPS en streaming continu.

    F-11 : un navigateur ne peut pas poser de header Authorization sur une
    connexion WebSocket, d'où le jeton passé en query string (?token=...),
    validé ici via authenticate_token avant d'accepter la connexion.
    Codes de fermeture : 4401 non authentifié, 4429 limite de débit,
    1011 service indisponible. Vérification d'accès à la livraison : module 1.
    """
    if not token:
        await websocket.close(code=WS_CLOSE_CODES[401])
        return
    try:
        utilisateur = await authenticate_token(websocket, token)
        await consume(websocket, "ws_connect", f"user:{utilisateur.id}")
    except HTTPException as exc:
        await websocket.close(code=ws_close_code(exc.status_code))
        return

    await gestionnaire_suivi.connecter(livraison_id, websocket)
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(f'{{"type": "ack", "message": "Position reçue: {data}"}}')
    except WebSocketDisconnect:
        gestionnaire_suivi.deconnecter(livraison_id, websocket)
    except Exception:
        gestionnaire_suivi.deconnecter(livraison_id, websocket)
