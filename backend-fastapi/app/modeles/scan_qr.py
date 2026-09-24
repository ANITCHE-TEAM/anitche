from typing import Optional
from pydantic import BaseModel, Field


class DemandeScanQR(BaseModel):
    qr_data: str = Field(description="Chaîne brute issue du scan QR (URL ou code format PAS-2026-XXXXXXXX)")


class ReponseScanPasseport(BaseModel):
    valide: bool
    code_passeport: str
    produit_id: Optional[int] = None
    produit_nom: str
    boutique_nom: str
    artisan_createur: Optional[str] = None
    origine_geographique: str
    materiaux_utilises: str
    statut_certification: str
    statut_certification_libelle: str
    url_verification: str
    nb_scans: int = 1
    message: str
