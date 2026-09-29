"""Conseiller shopping (docs/MODULE_IA.md).

- POST /ia/conseil : conseil argumenté selon la conversation, l'occasion,
  le style, le budget et les catégories ;
- POST /ia/recommandations : sélection selon les catégories et le budget.

Routes authentifiées (tout rôle : les réponses ne contiennent que le
catalogue public), limitées par utilisateur (scopes `ai_advice` et
`ai_recommendations`). AI_ENABLED=false : 503 conseiller_desactive, avant
l'authentification et le comptage.
"""
from fastapi import APIRouter, Depends, Request

from app.core.errors import CodedHTTPException, Error
from app.core.rate_limit import rate_limit
from app.modeles.conseiller_ia import DemandeConseil, DemandeRecommandations, ReponseConseil, ReponseRecommandations
from app.services.conseiller import service

router = APIRouter(prefix="/ia", tags=["Conseiller Shopping IA"])

UNAVAILABLE_RESPONSES = {
    503: {
        "model": Error,
        "description": "`errors.code` : `conseiller_desactive` (conseiller coupé : masquer l'entrée) ou "
        "`conseiller_indisponible` (catalogue injoignable : réessayer plus tard). Sans code : "
        "Redis ou vérification du jeton indisponible.",
    },
}


async def conseiller_actif(request: Request) -> None:
    if not request.app.state.settings.ai_enabled:
        raise CodedHTTPException(503, service.DISABLED_MESSAGE, service.DISABLED_CODE)


@router.post(
    "/conseil",
    response_model=ReponseConseil,
    summary="Conseil argumenté et produits du catalogue",
    dependencies=[Depends(conseiller_actif), Depends(rate_limit("ai_advice", key="user"))],
    responses=UNAVAILABLE_RESPONSES,
)
async def conseiller_shopping(request: Request, demande: DemandeConseil):
    """Produits visibles et en stock choisis dans le catalogue selon la
    conversation (tenue par le frontend), l'occasion, le style, le budget
    et les catégories, avec une justification par produit."""
    return await service.conseiller(request.app.state, demande)


@router.post(
    "/recommandations",
    response_model=ReponseRecommandations,
    summary="Sélection selon les catégories et le budget",
    dependencies=[Depends(conseiller_actif), Depends(rate_limit("ai_recommendations", key="user"))],
    responses=UNAVAILABLE_RESPONSES,
)
async def obtenir_recommandations(request: Request, demande: DemandeRecommandations):
    """Produits visibles et en stock des catégories demandées (tout le
    catalogue sans catégorie), au plus au prix du budget."""
    return await service.recommander(request.app.state, demande)
