"""Droits sur le suivi GPS d'une livraison, calqués sur Django.

Une seule requête SQL (rôle PostgreSQL en lecture seule, droits par colonne :
infra/postgres/fastapi_readonly.sql) sert aux quatre usages : publication,
lecture HTTP, connexion WebSocket et revalidation. Les règles sont des
fonctions pures (`can_publish`, `can_listen`), testées sans base.

Rôle et compte actif sont lus en base, pas dans le jeton (cache de 30 s) :
le retrait d'un livreur (rôle repassé à « client ») a un effet immédiat,
comme dans Django, où `Livraison.livreur_id` reste renseigné jusqu'à la
réassignation.
"""
import logging
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

import asyncpg
from fastapi import HTTPException

from app.core.errors import SERVICE_UNAVAILABLE_MESSAGE

logger = logging.getLogger("anitche.fastapi.tracking")

IN_PROGRESS = "en_cours"
COURIER = "livreur"
# ROLES_ADMINISTRATION côté Django (apps/vendeurs/permissions.py).
ADMINISTRATION_ROLES = frozenset({"admin", "super_admin"})

# Colonnes explicites : un SELECT * est refusé par les droits par colonne.
# $1 : UUID validé par Pydantic, $2 : identifiant de l'utilisateur.
# Aucune ligne : livraison inexistante. u.role nul : compte supprimé.
ACCESS_QUERY = """
SELECT l.status, l.livreur_id, c.client_id, u.role, u.is_active,
       g.livraison_latitude, g.livraison_longitude
FROM livraison_livraison AS l
JOIN commandes_commande AS c ON c.id = l.commande_id
LEFT JOIN commandes_groupecommande AS g ON g.id = c.groupe_id
LEFT JOIN utilisateurs_utilisateur AS u ON u.id = $2
WHERE l.id = $1
"""


class Refusal(StrEnum):
    """Motif de refus, renvoyé tel quel au frontend (`errors.code`)."""

    DELIVERY_NOT_FOUND = "livraison_introuvable"
    COURIERS_ONLY = "acces_reserve_livreurs"
    NOT_ASSIGNED = "livraison_non_assignee"
    NOT_IN_PROGRESS = "livraison_pas_en_cours"


@dataclass(frozen=True)
class DeliveryAccess:
    status: str
    courier_id: int | None
    client_id: int
    user_role: str | None
    user_active: bool
    # Point GPS donné par le client au checkout, ou None.
    destination: tuple[float, float] | None


async def fetch_access(db, delivery_id: UUID, user_id: int) -> DeliveryAccess | None:
    """Ligne d'accès, None si la livraison n'existe pas. HTTPException 503 si
    PostgreSQL est indisponible ou si un droit manque au rôle."""
    try:
        row = await db.fetchrow(ACCESS_QUERY, delivery_id, user_id)
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError) as exc:
        logger.error("Requête d'accès au suivi impossible : %s", type(exc).__name__)
        raise HTTPException(503, SERVICE_UNAVAILABLE_MESSAGE) from None
    if row is None:
        return None
    latitude, longitude = row["livraison_latitude"], row["livraison_longitude"]
    return DeliveryAccess(
        status=row["status"],
        courier_id=row["livreur_id"],
        client_id=row["client_id"],
        user_role=row["role"],
        user_active=bool(row["is_active"]),
        destination=None if latitude is None or longitude is None else (float(latitude), float(longitude)),
    )


def can_publish(access: DeliveryAccess | None, user_id: int) -> Refusal | None:
    """Publier une position : le livreur assigné, pendant `en_cours`.

    L'administration ne publie pas (Django : elle ne fait pas les étapes du
    livreur). Le contrôle « jamais le propriétaire de la boutique » n'est pas
    repris : garanti par Django à l'assignation.
    """
    if access is None:
        return Refusal.DELIVERY_NOT_FOUND
    if access.user_role != COURIER or not access.user_active:
        return Refusal.COURIERS_ONLY
    if access.courier_id != user_id:
        return Refusal.NOT_ASSIGNED
    if access.status != IN_PROGRESS:
        return Refusal.NOT_IN_PROGRESS
    return None


def can_listen(access: DeliveryAccess | None, user_id: int) -> Refusal | None:
    """Lire la position (HTTP ou WebSocket) : même périmètre que
    `livraisons_visibles` de Django, dans le même ordre, puis `en_cours`.

    - administration : toutes les livraisons ;
    - livreur : celles qui lui sont assignées ;
    - tout autre rôle (client, vendeur, moderateur, support) : celles de ses
      propres commandes. Le vendeur n'écoute donc pas les livraisons de sa
      boutique, seulement celles de ses achats.
    Hors périmètre : DELIVERY_NOT_FOUND (404, sans révéler l'existence).
    """
    if access is None or access.user_role is None or not access.user_active:
        return Refusal.DELIVERY_NOT_FOUND
    if access.user_role in ADMINISTRATION_ROLES:
        visible = True
    elif access.user_role == COURIER:
        visible = access.courier_id == user_id
    else:
        visible = access.client_id == user_id
    if not visible:
        return Refusal.DELIVERY_NOT_FOUND
    if access.status != IN_PROGRESS:
        return Refusal.NOT_IN_PROGRESS
    return None
