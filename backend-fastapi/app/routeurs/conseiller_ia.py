from fastapi import APIRouter
from app.modeles.conseiller_ia import (
    DemandeConseilIA,
    ReponseConseilIA,
    DemandeRecommandations,
    ReponseRecommandations,
)
from app.services.ia_service import ServiceConseillerIA

router = APIRouter(prefix="/ia", tags=["Conseiller Shopping IA"])


@router.post(
    "/conseil",
    response_model=ReponseConseilIA,
    summary="Obtenir des conseils de style et suggestions de produits par l'IA",
)
def conseiller_shopping(demande: DemandeConseilIA):
    """Analyse la requête de l'utilisateur et génère des recommandations expertes en mode, artisanat et culture ivoirienne."""
    return ServiceConseillerIA.generer_conseil(demande)


@router.post(
    "/recommandations",
    response_model=ReponseRecommandations,
    summary="Recommandations personnalisées selon les préférences",
)
def obtenir_recommandations(demande: DemandeRecommandations):
    """Génère une sélection sur-mesure d'articles selon le budget et les catégories sélectionnées."""
    return ServiceConseillerIA.generer_recommandations(demande)