from typing import Optional
from pydantic import BaseModel, Field


class PositionGPS(BaseModel):
    livraison_id: str = Field(description="Identifiant UUID de la livraison")
    livreur_id: int = Field(description="Identifiant de l'utilisateur livreur")
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    vitesse_kmh: Optional[float] = 0.0
    cap_degres: Optional[float] = 0.0
    horodatage: Optional[str] = None


class ReponsePositionLivreur(BaseModel):
    livraison_id: str
    livreur_id: int
    latitude: float
    longitude: float
    statut: str
    temps_estime_minutes: int
    distance_restante_km: float
    derniere_mise_a_jour: str
