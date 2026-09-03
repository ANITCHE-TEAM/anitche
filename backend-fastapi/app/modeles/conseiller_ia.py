from typing import List, Optional, Literal
from pydantic import BaseModel, Field


class MessageChat(BaseModel):
    role: Literal["user", "assistant", "system"]
    contenu: str


class DemandeConseilIA(BaseModel):
    messages: List[MessageChat]
    style: Optional[str] = Field(default=None, description="Style souhaité (traditionnel, moderne, chic, wax, baoulé)")
    budget_max: Optional[float] = Field(default=None, description="Budget maximum en FCFA")
    occasion: Optional[str] = Field(default=None, description="Occasion (mariage, fête, cadeau, quotidien)")


class ProduitConseil(BaseModel):
    id: int
    nom: str
    boutique: str
    prix: float
    image_url: Optional[str] = None
    justification: str


class ReponseConseilIA(BaseModel):
    reponse: str
    produits_suggeres: List[ProduitConseil] = []
    conseils_style: List[str] = []


class DemandeRecommandations(BaseModel):
    utilisateur_id: Optional[int] = None
    categories_preferees: List[str] = []
    budget_max: Optional[float] = None


class ReponseRecommandations(BaseModel):
    recommandations: List[ProduitConseil]
