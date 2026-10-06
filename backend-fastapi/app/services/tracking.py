"""Suivi GPS dans Redis : dernière position et diffusion.

Aucun état en mémoire partagé entre requêtes : plusieurs workers et
plusieurs instances voient la même position (base Redis 2 de FastAPI).

- Dernière position : clé `fastapi:gps:v1:pos:<livraison>`, JSON avec le
  livreur qui l'a publiée, expirée après TRACKING_POSITION_TTL secondes.
- Diffusion : canal `fastapi:gps:v1:chan:<livraison>`, message SANS
  l'identifiant du livreur. SET et PUBLISH dans une même transaction.
- TrackingHub : UN abonnement pub/sub par processus (démarré par le
  lifespan). Abonnement au canal à la première écoute d'une livraison,
  désabonnement à la dernière. Chaque WebSocket a une file bornée : si le
  client ne suit pas, la plus ancienne position est jetée (seule la
  dernière compte). Connexion pub/sub perdue : toutes les écoutes du
  processus sont fermées (1011), le client se reconnecte.

Redis indisponible : 503 (HTTP) ou 1011 (WebSocket), jamais de repli en
mémoire. Aucune écriture dans PostgreSQL : pas d'historique des positions.
"""
import asyncio
import logging
from uuid import UUID

from fastapi import HTTPException
from pydantic import ValidationError
from redis.exceptions import RedisError

from app.core.errors import SERVICE_UNAVAILABLE_MESSAGE
from app.modeles.suivi_temps_reel import MessagePosition, PositionLivreur, PositionStockee
from app.services.delivery_access import IN_PROGRESS, DeliveryAccess

logger = logging.getLogger("anitche.fastapi.tracking")

KEY_PREFIX = "fastapi:gps:v1:"
REDIS_ERRORS = (RedisError, OSError)

# Délai d'attente d'un message pub/sub avant de reboucler, et attente
# (croissante) avant de retenter la connexion pub/sub après une panne.
POLL_TIMEOUT = 1.0
RECONNECT_DELAYS = (0.5, 1, 2, 5)


def position_key(delivery_id: UUID) -> str:
    return f"{KEY_PREFIX}pos:{delivery_id}"


def channel_name(delivery_id: UUID) -> str:
    return f"{KEY_PREFIX}chan:{delivery_id}"


def _unavailable(action: str, exc: BaseException) -> HTTPException:
    logger.error("Suivi GPS : Redis indisponible (%s) : %s", action, type(exc).__name__)
    return HTTPException(503, SERVICE_UNAVAILABLE_MESSAGE)


async def publish_position(redis, position: PositionStockee, ttl: int) -> None:
    """Enregistre la dernière position et la diffuse aux abonnés."""
    message = MessagePosition(**position.publique().model_dump()).model_dump_json()
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.set(position_key(position.livraison_id), position.model_dump_json(), ex=ttl)
            pipe.publish(channel_name(position.livraison_id), message)
            await pipe.execute()
    except REDIS_ERRORS as exc:
        raise _unavailable("publication", exc) from None


async def discard_position(redis, delivery_id: UUID) -> None:
    try:
        await redis.delete(position_key(delivery_id))
    except REDIS_ERRORS as exc:
        raise _unavailable("suppression", exc) from None


async def current_position(redis, delivery_id: UUID, access: DeliveryAccess) -> PositionLivreur | None:
    """Dernière position à montrer, ou None.

    Livraison sortie de `en_cours`, ou position d'un autre livreur que
    l'assigné actuel (réassignation) : la clé est supprimée, rien n'est
    montré. Sinon, elle expire d'elle-même.
    """
    try:
        raw = await redis.get(position_key(delivery_id))
    except REDIS_ERRORS as exc:
        raise _unavailable("lecture", exc) from None
    if raw is None:
        return None
    try:
        stored = PositionStockee.model_validate_json(raw)
    except ValidationError:
        logger.error("Suivi GPS : position illisible dans Redis, supprimée")
        stored = None
    if stored is None or access.status != IN_PROGRESS or stored.livreur_id != access.courier_id:
        await discard_position(redis, delivery_id)
        return None
    return stored.publique()


# ------------------------------------------------------------ diffusion


class Subscription:
    """Écoute d'une livraison par une WebSocket : file bornée de messages
    `position` déjà sérialisés. `CLOSED` signale la perte de l'abonnement."""

    CLOSED = None

    def __init__(self, delivery_id: UUID, maxsize: int):
        self.delivery_id = delivery_id
        self.channel = channel_name(delivery_id)
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize)

    def push(self, message: str) -> None:
        if self.queue.full():
            self.queue.get_nowait()  # la plus ancienne position est jetée
        self.queue.put_nowait(message)

    def close(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()
        self.queue.put_nowait(self.CLOSED)

    async def get(self) -> str | None:
        return await self.queue.get()


class TrackingHub:
    """Un abonnement pub/sub Redis par processus, partagé par ses WebSockets."""

    def __init__(self, redis, queue_size: int):
        self._redis = redis
        self._queue_size = queue_size
        self._pubsub = None
        self._subscribers: dict[str, set[Subscription]] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="tracking-hub")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            # asyncio.wait : voir _serve dans routeurs/suivi_temps_reel.py.
            await asyncio.wait([self._task])
            self._task = None
        self._close_all()
        await self._close_pubsub()

    async def subscribe(self, delivery_id: UUID) -> Subscription:
        """Abonne une WebSocket. HTTPException 503 si Redis est indisponible."""
        subscription = Subscription(delivery_id, self._queue_size)
        async with self._lock:
            listeners = self._subscribers.setdefault(subscription.channel, set())
            if not listeners:
                try:
                    await self._connected_pubsub().subscribe(subscription.channel)
                except REDIS_ERRORS as exc:
                    del self._subscribers[subscription.channel]
                    raise _unavailable("abonnement", exc) from None
            listeners.add(subscription)
        return subscription

    async def unsubscribe(self, subscription: Subscription) -> None:
        async with self._lock:
            listeners = self._subscribers.get(subscription.channel)
            if listeners is None or subscription not in listeners:
                return
            listeners.discard(subscription)
            if listeners:
                return
            del self._subscribers[subscription.channel]
            if self._pubsub is None:
                return
            try:
                await self._pubsub.unsubscribe(subscription.channel)
            except REDIS_ERRORS as exc:
                # La boucle de lecture constate la panne et repart à neuf.
                logger.warning("Suivi GPS : désabonnement impossible : %s", type(exc).__name__)

    def listener_count(self, delivery_id: UUID) -> int:
        return len(self._subscribers.get(channel_name(delivery_id), ()))

    # -------------------------------------------------------- interne

    def _connected_pubsub(self):
        if self._pubsub is None:
            self._pubsub = self._redis.pubsub()
        return self._pubsub

    async def _run(self) -> None:
        failures = 0
        while True:
            try:
                pubsub = self._connected_pubsub()
                await pubsub.connect()
                while True:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=POLL_TIMEOUT)
                    failures = 0
                    if message is not None and message.get("type") == "message":
                        self._dispatch(message["channel"], message["data"])
                    # Laisse la main si le message était déjà disponible.
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # panne Redis ou erreur inattendue : on repart à neuf
                logger.error("Suivi GPS : abonnement Redis perdu : %s", type(exc).__name__)
                async with self._lock:
                    self._close_all()
                    await self._close_pubsub()
                await asyncio.sleep(RECONNECT_DELAYS[min(failures, len(RECONNECT_DELAYS) - 1)])
                failures += 1

    def _dispatch(self, channel: str, data: str) -> None:
        listeners = self._subscribers.get(channel)
        if not listeners:
            return
        try:
            # Validé à la réception : un message malformé n'atteint aucun client.
            message = MessagePosition.model_validate_json(data).model_dump_json()
        except ValidationError:
            logger.error("Suivi GPS : message illisible sur %s, ignoré", channel)
            return
        for subscription in list(listeners):
            subscription.push(message)

    def _close_all(self) -> None:
        for listeners in self._subscribers.values():
            for subscription in listeners:
                subscription.close()
        self._subscribers.clear()

    async def _close_pubsub(self) -> None:
        pubsub, self._pubsub = self._pubsub, None
        if pubsub is not None:
            try:
                await pubsub.aclose()
            except REDIS_ERRORS:
                pass
