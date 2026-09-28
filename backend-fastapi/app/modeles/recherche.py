"""Contrat de la recherche (docs/MODULE_RECHERCHE.md).

Liste alignée sur GET /api/catalogue/produits/ de Django (même enveloppe
count/next/previous/results, mêmes champs par produit), plus les facettes.
Aucun champ de stock exact : seulement `en_stock`.
"""
from typing import Literal

from pydantic import BaseModel, Field


class ProduitRecherche(BaseModel):
    """Mêmes champs que ProduitPublicListSerializer (Django)."""

    id: int
    nom: str
    slug: str
    prix_base: str = Field(description="Prix de base en FCFA, chaîne à 2 décimales (« 4500.00 »), comme Django")
    prix_min: float = Field(description="Prix affiché : plus petit prix effectif (promo comprise) des variantes actives")
    image_principale: str | None = Field(description="URL absolue de l'image principale, ou null")
    categorie: int | None
    categorie_nom: str | None = Field(description="null si le produit n'a pas de catégorie (Django omet alors la clé)")
    boutique: int
    boutique_nom: str
    boutique_slug: str
    en_stock: bool = Field(description="Au moins une variante active en stock (jamais la quantité)")
    date_creation: str = Field(description="ISO 8601, UTC (« …Z »)")


class FacetteCategorie(BaseModel):
    id: int
    nom: str
    slug: str
    parent: int | None = Field(description="Catégorie parente, null pour une catégorie racine")
    nombre: int


class FacetteBoutique(BaseModel):
    id: int
    nom: str
    slug: str
    nombre: int


class TranchePrix(BaseModel):
    min: int = Field(description="Borne basse incluse (FCFA)")
    max: int | None = Field(description="Borne haute exclue (FCFA), null pour la dernière tranche")
    nombre: int


class FacettePrix(BaseModel):
    min: float | None
    max: float | None
    tranches: list[TranchePrix] = Field(description="Tranches non vides seulement")


class FacettesRecherche(BaseModel):
    categories: list[FacetteCategorie] = Field(description="Catégories directes des résultats (20 au plus)")
    boutiques: list[FacetteBoutique] = Field(description="Boutiques des résultats (10 au plus)")
    prix: FacettePrix


class ReponseRecherche(BaseModel):
    count: int = Field(description="Total des résultats (peut avoir 60 s de retard : cache)")
    next: str | None
    previous: str | None
    results: list[ProduitRecherche]
    facettes: FacettesRecherche | None = Field(description="Calculées sur tout l'ensemble filtré, en page 1 seulement")


class Suggestion(BaseModel):
    type: Literal["categorie", "boutique", "produit"]
    texte: str
    id: int
    slug: str


class ReponseSuggestions(BaseModel):
    requete: str
    suggestions: list[Suggestion]
