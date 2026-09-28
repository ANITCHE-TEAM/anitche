"""Suivi GPS temps réel d'une livraison `en_cours` (docs/MODULE_SUIVI_GPS.md).

- POST /livraison/position : le livreur assigné publie sa position ;
- GET /livraison/position/{livraison_id} : dernière position (client de la
  commande, livreur assigné, administration) ;
- WS /livraison/ws/{livraison_id} : positions en direct, authentification
  par premier message (jamais de jeton dans l'URL).

Chaque refus porte un code machine (`errors.code[0]` en HTTP, code de
fermeture en WebSocket) : le frontend ne lit jamais le texte français.
"""
import asyncio
import logging
import time
from collections import deque
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.core.auth import CurrentUser, authenticate_token, get_current_user
from app.core.errors import (
    WS_CLOSE_INTERNAL_ERROR,
    WS_CLOSE_MESSAGE_TOO_BIG,
    WS_CLOSE_NORMAL,
    WS_CLOSE_POLICY_VIOLATION,
    CodedHTTPException,
    ws_close_code,
)
from app.core.rate_limit import consume, rate_limit
from app.modeles.suivi_temps_reel import (
    MessageAuth,
    MessageAuthentifie,
    MessageFinSuivi,
    MessagePosition,
    PositionLivreur,
    PositionPublication,
    PositionStockee,
)
from app.services import eta, tracking
from app.services.delivery_access import (
    COURIER,
    DeliveryAccess,
    Refusal,
    can_listen,
    can_publish,
    fetch_access,
)

logger = logging.getLogger("anitche.fastapi.tracking")

router = APIRouter(prefix="/livraison", tags=["Suivi GPS temps réel"])

NO_POSITION_CODE = "aucune_position"
NO_POSITION_MESSAGE = "Aucune position disponible pour cette livraison."
# Mêmes codes HTTP et textes que Django (/api/livraison/<id>/ et statut/).
REFUSALS = {
    Refusal.DELIVERY_NOT_FOUND: (404, "Livraison introuvable."),
    Refusal.COURIERS_ONLY: (403, "Accès réservé aux livreurs."),
    Refusal.NOT_ASSIGNED: (403, "Vous n'êtes pas autorisé à modifier cette livraison."),
    Refusal.NOT_IN_PROGRESS: (409, "La livraison n'est pas en cours de livraison."),
}

WS_CLOSE_UNAUTHENTICATED = 4401
WS_CLOSE_FORBIDDEN = 4403
WS_CLOSE_TOO_MANY_MESSAGES = 4429


def _refused(refusal: Refusal) -> CodedHTTPException:
    status_code, detail = REFUSALS[refusal]
    return CodedHTTPException(status_code, detail, refusal.value)


def _no_position() -> CodedHTTPException:
    return CodedHTTPException(404, NO_POSITION_MESSAGE, NO_POSITION_CODE)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@router.post(
    "/position",
    response_model=PositionLivreur,
    summary="Publier la position du livreur assigné (livraison en cours)",
    dependencies=[Depends(rate_limit("gps_publish", key="user"))],
    responses={409: {"description": "La livraison n'est pas en cours (errors.code : livraison_pas_en_cours)."}},
)
async def publier_position(
    position: PositionPublication,
    request: Request,
    utilisateur: CurrentUser = Depends(get_current_user),
):
    """Le livreur est l'utilisateur du jeton, l'horodatage celui du serveur.
    Rythme conseillé : une position toutes les 5 s ; arrêt sur 403, 404, 409.

    Refus (`errors.code`) : 403 `acces_reserve_livreurs`, 403
    `livraison_non_assignee`, 404 `livraison_introuvable`, 409
    `livraison_pas_en_cours`.
    """
    # Filtre sans requête SQL ; le rôle en base reste la référence.
    if utilisateur.role != COURIER:
        raise _refused(Refusal.COURIERS_ONLY)

    state = request.app.state
    settings = state.settings
    access = await fetch_access(state.db, position.livraison_id, utilisateur.id)
    refusal = can_publish(access, utilisateur.id)
    if refusal is not None:
        raise _refused(refusal)

    distance_km, minutes = eta.estimate(
        position.latitude,
        position.longitude,
        access.destination,
        detour_factor=settings.eta_detour_factor,
        average_speed_kmh=settings.eta_average_speed_kmh,
    )
    stored = PositionStockee(
        livraison_id=position.livraison_id,
        livreur_id=utilisateur.id,
        latitude=position.latitude,
        longitude=position.longitude,
        vitesse_kmh=position.vitesse_kmh,
        cap_degres=position.cap_degres,
        horodatage=_utc_now(),
        distance_restante_km=distance_km,
        temps_estime_minutes=minutes,
    )
    await tracking.publish_position(state.redis, stored, settings.tracking_position_ttl)
    return stored.publique()


@router.get(
    "/position/{livraison_id}",
    response_model=PositionLivreur,
    summary="Dernière position du livreur (livraison en cours)",
    dependencies=[Depends(rate_limit("gps_read", key="user"))],
)
async def obtenir_position(
    livraison_id: UUID,
    request: Request,
    utilisateur: CurrentUser = Depends(get_current_user),
):
    """404 `livraison_introuvable` : hors périmètre ou inexistante (comme
    Django). 404 `aucune_position` : pas en cours, jamais publiée, expirée ou
    publiée par un ancien livreur ; rien à afficher, ce n'est pas une panne.
    Jamais de position par défaut."""
    state = request.app.state
    access = await fetch_access(state.db, livraison_id, utilisateur.id)
    refusal = can_listen(access, utilisateur.id)
    if refusal is Refusal.DELIVERY_NOT_FOUND:
        raise _refused(refusal)
    if refusal is Refusal.NOT_IN_PROGRESS:
        await tracking.discard_position(state.redis, livraison_id)
        raise _no_position()

    position = await tracking.current_position(state.redis, livraison_id, access)
    if position is None:
        raise _no_position()
    return position


# ------------------------------------------------------------ WebSocket


class _Close(Exception):
    """Fin de la connexion : code, motif court (123 octets au plus) et
    éventuel dernier message à envoyer avant la fermeture."""

    def __init__(self, code: int, reason: str = "", final_message: str | None = None):
        super().__init__(code)
        self.code = code
        self.reason = reason
        self.final_message = final_message


# Délai dépassé, autre message, JSON invalide pendant l'authentification.
_AUTH_EXPECTED = _Close(WS_CLOSE_UNAUTHENTICATED, "Authentification attendue.")


def _close_from_http(exc: HTTPException) -> _Close:
    # 401 -> 4401, 429 -> 4429, 503 et le reste -> 1011.
    reasons = {401: "Jeton invalide ou expiré.", 429: "Trop de requêtes."}
    return _Close(ws_close_code(exc.status_code), reasons.get(exc.status_code, "Service indisponible."))


class _Connection:
    """État d'une WebSocket authentifiée."""

    def __init__(self, websocket: WebSocket, delivery_id: UUID, user: CurrentUser, token: str):
        self.websocket = websocket
        self.delivery_id = delivery_id
        self.user = user
        self.token = token
        self.state = websocket.app.state
        self.settings = self.state.settings
        self.access: DeliveryAccess | None = None
        self.message_times: deque[float] = deque()


async def _receive_text(websocket: WebSocket, max_bytes: int, binary: _Close) -> str | None:
    """Texte reçu, None si le client s'est déconnecté. _Close(1009) si trop
    grand, `binary` pour un message binaire."""
    message = await websocket.receive()
    if message["type"] == "websocket.disconnect":
        return None
    text = message.get("text")
    if text is None:
        raise binary
    if len(text.encode()) > max_bytes:
        raise _Close(WS_CLOSE_MESSAGE_TOO_BIG, "Message trop grand.")
    return text


def _parse_auth(text: str) -> str:
    try:
        return MessageAuth.model_validate_json(text).token
    except ValidationError:
        raise _AUTH_EXPECTED from None


async def _authenticate(websocket: WebSocket, token: str) -> CurrentUser:
    try:
        return await authenticate_token(websocket, token)
    except HTTPException as exc:
        raise _close_from_http(exc) from None


async def _check_access(connection: _Connection) -> None:
    """Revalidation des droits : 4403 si perdus, fin_suivi puis 1000 si la
    livraison n'est plus en cours."""
    try:
        access = await fetch_access(connection.state.db, connection.delivery_id, connection.user.id)
    except HTTPException as exc:
        raise _close_from_http(exc) from None
    refusal = can_listen(access, connection.user.id)
    if refusal is Refusal.NOT_IN_PROGRESS:
        try:
            await tracking.discard_position(connection.state.redis, connection.delivery_id)
        except HTTPException:
            pass  # la clé expirera d'elle-même
        final = MessageFinSuivi(livraison_id=connection.delivery_id, statut=access.status).model_dump_json()
        raise _Close(WS_CLOSE_NORMAL, "Suivi terminé.", final_message=final)
    if refusal is not None:
        raise _Close(WS_CLOSE_FORBIDDEN, "Accès refusé.")
    connection.access = access


async def _renew_token(connection: _Connection, token: str) -> None:
    user = await _authenticate(connection.websocket, token)
    if user.id != connection.user.id:
        raise _Close(WS_CLOSE_UNAUTHENTICATED, "Jeton d'un autre compte.")
    connection.user = user
    connection.token = token
    await _check_access(connection)


async def _receive_loop(connection: _Connection) -> None:
    """Après l'authentification, seul `auth` (renouvellement du jeton) est
    accepté, 6 messages par minute au plus. Aucun écho."""
    window = 60.0
    limit = connection.settings.ws_client_messages_per_minute
    while True:
        text = await _receive_text(
            connection.websocket,
            connection.settings.ws_max_message_bytes,
            _Close(WS_CLOSE_POLICY_VIOLATION, "Message non prévu."),
        )
        if text is None:
            return
        now = time.monotonic()
        times = connection.message_times
        while times and now - times[0] >= window:
            times.popleft()
        times.append(now)
        if len(times) > limit:
            raise _Close(WS_CLOSE_TOO_MANY_MESSAGES, "Trop de messages.")
        try:
            token = MessageAuth.model_validate_json(text).token
        except ValidationError:
            raise _Close(WS_CLOSE_POLICY_VIOLATION, "Message non prévu.") from None
        await _renew_token(connection, token)


async def _revalidate_loop(connection: _Connection) -> None:
    while True:
        await asyncio.sleep(connection.settings.ws_revalidate_interval)
        await _authenticate(connection.websocket, connection.token)
        await _check_access(connection)


async def _forward_loop(connection: _Connection, subscription: tracking.Subscription) -> None:
    while True:
        message = await subscription.get()
        if message is tracking.Subscription.CLOSED:
            raise _Close(WS_CLOSE_INTERNAL_ERROR, "Service indisponible.")
        await connection.websocket.send_text(message)


async def _serve(connection: _Connection, subscription: tracking.Subscription) -> None:
    """Trois tâches ; la première qui s'arrête ferme la connexion. Une seule
    tâche envoie (_forward_loop) : pas d'envois concurrents."""
    tasks = [
        asyncio.create_task(_forward_loop(connection, subscription)),
        asyncio.create_task(_receive_loop(connection)),
        asyncio.create_task(_revalidate_loop(connection)),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        # asyncio.wait et non gather : si cette tâche est elle-même annulée
        # (arrêt du serveur), gather relèverait une CancelledError fabriquée
        # à partir d'un enfant, que la portée d'annulation appelante (anyio)
        # ne reconnaîtrait pas comme la sienne.
        await asyncio.wait(tasks)
        for task in tasks:
            if task.done() and not task.cancelled():
                task.exception()  # erreur récupérée : pas d'avertissement asyncio
    for task in done:
        task.result()  # relève _Close ou l'erreur de la tâche terminée


async def _open(websocket: WebSocket, delivery_id: UUID) -> _Connection:
    """Authentification par premier message, limite de connexions, droits."""
    settings = websocket.app.state.settings
    try:
        async with asyncio.timeout(settings.ws_auth_timeout):
            text = await _receive_text(websocket, settings.ws_max_message_bytes, _AUTH_EXPECTED)
    except TimeoutError:
        raise _AUTH_EXPECTED from None
    if text is None:
        raise WebSocketDisconnect()
    token = _parse_auth(text)
    user = await _authenticate(websocket, token)
    try:
        await consume(websocket, "ws_connect", f"user:{user.id}")
    except HTTPException as exc:
        raise _close_from_http(exc) from None

    connection = _Connection(websocket, delivery_id, user, token)
    try:
        access = await fetch_access(connection.state.db, delivery_id, user.id)
    except HTTPException as exc:
        raise _close_from_http(exc) from None
    if can_listen(access, user.id) is not None:
        raise _Close(WS_CLOSE_FORBIDDEN, "Accès refusé.")
    connection.access = access
    return connection


@router.websocket("/ws/{livraison_id}")
async def suivre_livraison(websocket: WebSocket, livraison_id: str):
    """Positions en direct pendant `en_cours`. Protocole et codes de
    fermeture : docs/MODULE_SUIVI_GPS.md. Un éventuel `?token=` est ignoré."""
    await websocket.accept()
    subscription = None
    hub: tracking.TrackingHub = websocket.app.state.tracking_hub
    try:
        try:
            delivery_id = UUID(livraison_id)
        except ValueError:
            raise _Close(WS_CLOSE_POLICY_VIOLATION, "Identifiant de livraison invalide.") from None

        connection = await _open(websocket, delivery_id)
        # Abonnement AVANT la lecture de la dernière position : aucune n'est
        # perdue, un doublon est possible (le client garde la plus récente).
        try:
            subscription = await hub.subscribe(delivery_id)
            await websocket.send_text(MessageAuthentifie(livraison_id=delivery_id).model_dump_json())
            last = await tracking.current_position(connection.state.redis, delivery_id, connection.access)
        except HTTPException as exc:
            raise _close_from_http(exc) from None
        if last is not None:
            await websocket.send_text(MessagePosition(**last.model_dump()).model_dump_json())
        await _serve(connection, subscription)
    except _Close as close:
        await _close(websocket, close)
    except WebSocketDisconnect:
        pass
    except Exception:
        # Jamais le jeton dans le journal : seul le type et la pile.
        logger.exception("Suivi GPS : erreur sur la WebSocket")
        await _close(websocket, _Close(WS_CLOSE_INTERNAL_ERROR, "Erreur interne."))
    finally:
        if subscription is not None:
            await hub.unsubscribe(subscription)


async def _close(websocket: WebSocket, close: _Close) -> None:
    try:
        if close.final_message is not None:
            await websocket.send_text(close.final_message)
        await websocket.close(code=close.code, reason=close.reason)
    except (RuntimeError, WebSocketDisconnect, OSError):
        pass  # client déjà parti
