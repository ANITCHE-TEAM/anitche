from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class ProduitSearchResult(BaseModel):
    id: int
    nom: str
    slug: str
    boutique_id: int
    boutique_nom: str
    prix: float
    image_url: Optional[str] = None
    categorie_nom: str
    note_moyenne: float = 5.0
    disponible: bool = True
    description: Optional[str] = ""


class FacettesRecherche(BaseModel):
    categories: List[Dict[str, Any]] = []
    fourchettes_prix: Dict[str, float] = {"min": 0.0, "max": 100000.0}
    boutiques: List[Dict[str, Any]] = []


class ReponseRecherche(BaseModel):
    total: int
    page: int
    par_page: int
    pages_total: int
    resultats: List[ProduitSearchResult]
    facettes: FacettesRecherche


class SuggestionItem(BaseModel):
    texte: str
    type: str  # 'produit', 'categorie', 'boutique', 'artisanat'
    id: Optional[int] = None
    score: float = 1.0


class ReponseSuggestions(BaseModel):
    requete: str
    suggestions: List[SuggestionItem]
