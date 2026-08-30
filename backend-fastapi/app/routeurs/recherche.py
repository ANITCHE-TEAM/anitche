from typing import Optional
from fastapi import APIRouter, Query
from app.modeles.recherche import ReponseRecherche, ReponseSuggestions
from app.services.recherche_service import ServiceRecherche

router = APIRouter(prefix="/recherche", tags=["Recherche & Suggestions"])


@router.get(
    "/produits",
    response_model=ReponseRecherche,
    summary="Recherche multi-critères et filtrage rapide du catalogue",
)
def rechercher_produits(
    q: Optional[str] = Query(None, description="Termes de recherche (nom, description, artisanat)"),
    categorie: Optional[str] = Query(None, description="Filtre par catégorie"),
    prix_min: Optional[float] = Query(None, ge=0, description="Prix minimum en FCFA"),
    prix_max: Optional[float] = Query(None, ge=0, description="Prix maximum en FCFA"),
    boutique_id: Optional[int] = Query(None, description="Filtrer par boutique"),
    tri: Optional[str] = Query("pertinence", description="Option de tri (pertinence, prix_asc, prix_desc, note)"),
    page: int = Query(1, ge=1, description="Numéro de page"),
    par_page: int = Query(10, ge=1, le=50, description="Nombre de résultats par page"),
):
    """Effectue une recherche plein texte ultra-rapide avec calcul automatique des facettes et pagination."""
    return ServiceRecherche.rechercher_produits(
        q=q,
        categorie=categorie,
        prix_min=prix_min,
        prix_max=prix_max,
        boutique_id=boutique_id,
        tri=tri,
        page=page,
        par_page=par_page,
    )


@router.get(
    "/suggestions",
    response_model=ReponseSuggestions,
    summary="Suggestions instantanées d'autocomplétion pendant la frappe",
)
def obtenir_suggestions(
    q: str = Query(..., min_length=2, description="Début du mot tapé par l'utilisateur"),
    limite: int = Query(5, ge=1, le=20, description="Nombre maximum de suggestions"),
):
    """Retourne des suggestions en temps réel pour l'autocomplétion de la barre de recherche."""
    return ServiceRecherche.suggerer(q=q, limite=limite)