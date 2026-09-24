"""Authentification/autorisation du microservice FastAPI, déléguée à Django.

Ce service n'a pas sa propre base d'utilisateurs : on valide le JWT émis par
Django (SimpleJWT) en interrogeant son endpoint /profil/, plutôt que de
dupliquer la logique d'authentification ici. Corrige F-11 (audit sécurité) :
suivi_temps_reel.py n'appliquait auparavant aucun contrôle d'accès.
"""
import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.core.config import settings

bearer_scheme = HTTPBearer()


async def verifier_jwt_brut(token: str) -> dict:
    """Fonction bas niveau partagée : valide un JWT brut auprès de Django.

    Utilisée à la fois par utilisateur_courant() (HTTP, via HTTPBearer) et
    par le WebSocket de suivi (qui ne peut pas poser de header Authorization
    standard côté navigateur, d'où le besoin d'une fonction acceptant le
    token brut directement).
    """
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{settings.django_api_base_url}/utilisateurs/profil/",
                headers={"Authorization": f"Bearer {token}"},
                timeout=3.0,
            )
        except httpx.RequestError:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Service d'authentification indisponible.",
            )

    if response.status_code != 200:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Jeton invalide ou expiré.")

    utilisateur = response.json()  # contient au minimum id, role
    utilisateur["_token"] = token
    return utilisateur


async def utilisateur_courant(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """Valide le JWT émis par Django en interrogeant son endpoint /profil/
    (réutilise l'authentification SimpleJWT existante sans la dupliquer)."""
    return await verifier_jwt_brut(credentials.credentials)


async def verifier_acces_livraison(livraison_id: str, utilisateur: dict) -> None:
    """Vérifie que l'utilisateur authentifié a le droit de voir/mettre à jour
    cette livraison précise, en réutilisant les règles déjà appliquées côté
    Django (LivraisonDetailView.get_queryset scoping par rôle)."""
    if utilisateur.get("role") in ("admin", "super_admin"):
        return

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{settings.django_api_base_url}/livraison/{livraison_id}/",
                headers={"Authorization": f"Bearer {utilisateur['_token']}"},
                timeout=3.0,
            )
        except httpx.RequestError:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Service de vérification des livraisons indisponible.",
            )

    if response.status_code == 404:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Livraison introuvable ou accès refusé.",
        )
    if response.status_code != 200:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Accès refusé à cette livraison.",
        )