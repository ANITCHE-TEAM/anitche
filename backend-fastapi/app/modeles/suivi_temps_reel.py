"""Suivi GPS : corps reçus, réponses et messages WebSocket.

Champs JSON en français, comme Django. Nombres finis uniquement (NaN et
Infinity ne sont pas du JSON : `JSON.parse` échouerait chez les abonnés).
Contrat complet : docs/MODULE_SUIVI_GPS.md.
"""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PositionPublication(BaseModel):
    """Corps de POST /livraison/position.

    Le livreur est l'utilisateur du jeton et l'horodatage est celui du
    serveur : `livreur_id` et `horodatage` envoyés par d'anciens clients sont
    ignorés, comme tout champ inconnu.
    """

    model_config = ConfigDict(extra="ignore")

    livraison_id: UUID
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)
    vitesse_kmh: float | None = Field(default=None, ge=0, le=200, allow_inf_nan=False)
    cap_degres: float | None = Field(default=None, ge=0, lt=360, allow_inf_nan=False)


class PositionLivreur(BaseModel):
    """Position montrée au client et à l'administration (jamais l'identifiant
    du livreur). Distance et temps : estimation indicative, null si le
    client n'a pas donné son point GPS au checkout."""

    livraison_id: UUID
    latitude: float
    longitude: float
    vitesse_kmh: float | None
    cap_degres: float | None
    horodatage: str = Field(description="Heure du serveur à la réception, UTC, ISO 8601 (« …Z »).")
    distance_restante_km: float | None = Field(
        description="Indicative : vol d'oiseau × facteur de détour. null sans point GPS du client."
    )
    temps_estime_minutes: int | None = Field(
        description="Indicatif : vitesse moyenne urbaine, sans trafic. null sans point GPS du client."
    )


class PositionStockee(PositionLivreur):
    """Dernière position dans Redis : avec le livreur qui l'a publiée, pour
    ne jamais montrer celle d'un ancien livreur après une réassignation."""

    livreur_id: int

    def publique(self) -> PositionLivreur:
        return PositionLivreur.model_validate(self.model_dump(exclude={"livreur_id"}))


# ------------------------------------------------------------ WebSocket


class MessageAuth(BaseModel):
    """Seul message accepté du client : authentification (premier message)
    ou renouvellement du jeton."""

    type: Literal["auth"]
    token: str = Field(min_length=1, max_length=4096)


class MessageAuthentifie(BaseModel):
    type: Literal["authentifie"] = "authentifie"
    livraison_id: UUID


class MessagePosition(PositionLivreur):
    type: Literal["position"] = "position"


class MessageFinSuivi(BaseModel):
    type: Literal["fin_suivi"] = "fin_suivi"
    livraison_id: UUID
    statut: str
