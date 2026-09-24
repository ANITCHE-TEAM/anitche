from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from app.modeles.suivi_temps_reel import PositionGPS, ReponsePositionLivreur
from app.services.websocket_manager import gestionnaire_suivi
from app.core.securite import utilisateur_courant, verifier_acces_livraison, verifier_jwt_brut

router = APIRouter(prefix="/livraison", tags=["Suivi Temps Réel & Télémétrie GPS"])


@router.post(
    "/position",
    response_model=ReponsePositionLivreur,
    summary="Mettre à jour la position GPS du livreur en temps réel",
)
async def mettre_a_jour_position(
    position: PositionGPS,
    utilisateur: dict = Depends(utilisateur_courant),
):
    """Reçoit les coordonnées du livreur et les diffuse instantanément aux clients connectés via WebSockets.

    F-11 (audit sécurité) : authentification requise, et seul le livreur
    concerné (ou un admin) peut publier une position pour cette livraison —
    sans quoi n'importe qui pourrait injecter de fausses coordonnées GPS
    pour une livraison qui n'est pas la sienne.
    """
    if utilisateur.get("role") not in ("livreur", "admin", "super_admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Accès réservé aux livreurs.")
    if utilisateur.get("role") == "livreur" and utilisateur.get("id") != position.livreur_id:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Un livreur ne peut publier que sa propre position.",
        )
    await verifier_acces_livraison(position.livraison_id, utilisateur)
    return await gestionnaire_suivi.diffuser_position(position)


@router.get(
    "/position/{livraison_id}",
    response_model=ReponsePositionLivreur,
    summary="Obtenir la dernière position connue et le temps restant estimé",
)
async def obtenir_derniere_position(
    livraison_id: str,
    utilisateur: dict = Depends(utilisateur_courant),
):
    """Retourne la position GPS la plus récente ainsi que la distance et le temps d'arrivée estimé.

    F-11 : authentification requise, scopée par rôle via Django
    (LivraisonDetailView.get_queryset), pour éviter qu'un tiers puisse
    espionner la position de n'importe quelle livraison.
    """
    await verifier_acces_livraison(livraison_id, utilisateur)
    return gestionnaire_suivi.obtenir_derniere_position(livraison_id)


@router.websocket("/ws/{livraison_id}")
async def websocket_suivi_livraison(websocket: WebSocket, livraison_id: str, token: str = ""):
    """Canal WebSocket bidirectionnel pour recevoir les coordonnées GPS en streaming continu.

    F-11 : un navigateur ne peut pas poser de header Authorization sur une
    connexion WebSocket, d'où le jeton passé en query string (?token=...),
    validé ici via verifier_jwt_brut avant d'accepter la connexion.
    """
    if not token:
        await websocket.close(code=4401)
        return
    try:
        utilisateur = await verifier_jwt_brut(token)
        await verifier_acces_livraison(livraison_id, utilisateur)
    except HTTPException:
        await websocket.close(code=4401)
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
