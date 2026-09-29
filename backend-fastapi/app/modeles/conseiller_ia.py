"""Contrat du conseiller IA (docs/MODULE_IA.md).

Demandes : champs fixes (tout champ inconnu est refusé : 400
« Ce champ n'est pas autorisé. »), textes nettoyés (composition Unicode
NFC, espaces fusionnés) PUIS mesurés, budget en FCFA entiers comme la
recherche. Aucun identifiant d'utilisateur : c'est le jeton qui identifie.

Réponses : chaque produit a exactement les champs d'un résultat de
GET /recherche/produits (même carte produit côté frontend), plus sa
justification. `source` : « regles » (fournisseur simulé ou repli) ou
« ia » ; jamais le nom du fournisseur.
"""
import re
import unicodedata
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, field_validator
from pydantic_core import PydanticCustomError

from app.modeles.recherche import ProduitRecherche
from app.services.search import MAX_PRICE

MAX_MESSAGES = 10
MAX_CONTENT_LENGTH = 1000
MAX_CONTEXT_LENGTH = 60
MAX_CATEGORIES = 5
MAX_CATEGORY_LENGTH = 120  # SlugField de la catégorie (Django)

CONTROL_CHARACTERS_MESSAGE = "Ce champ contient des caractères non autorisés."
LAST_MESSAGE_MESSAGE = "Le dernier message doit être celui du client (role « user »)."
# Même motif et même texte que le filtre `categorie` de la recherche.
SLUG_PATTERN = re.compile(r"^[-a-zA-Z0-9_]+$")
SLUG_MESSAGE = "Ce champ ne doit contenir que des lettres, des nombres, des tirets bas _ et des traits d'union."

Source = Literal["regles", "ia"]


def _clean(value: str, max_length: int) -> str:
    """Caractères de contrôle refusés (hors espaces : tabulation, retour à la
    ligne), puis NFC et espaces fusionnés, puis longueur vérifiée."""
    if any(unicodedata.category(char) == "Cc" and not char.isspace() for char in value):
        raise PydanticCustomError("caracteres_interdits", CONTROL_CHARACTERS_MESSAGE)
    text = " ".join(unicodedata.normalize("NFC", value).split())
    if len(text) > max_length:
        raise PydanticCustomError(
            "string_too_long", "Texte trop long", {"max_length": max_length},
        )
    return text


def _content(value: str) -> str:
    text = _clean(value, MAX_CONTENT_LENGTH)
    if not text:
        raise PydanticCustomError("string_too_short", "Texte vide", {"min_length": 1})
    return text


def _context(value: str | None) -> str | None:
    if value is None:
        return None
    return _clean(value, MAX_CONTEXT_LENGTH) or None


def _category(value: str) -> str:
    if not SLUG_PATTERN.match(value):
        raise PydanticCustomError("categorie_invalide", SLUG_MESSAGE)
    return value


Category = Annotated[str, Field(max_length=MAX_CATEGORY_LENGTH), AfterValidator(_category)]
Budget = Annotated[
    StrictInt | None,
    Field(default=None, ge=1, le=MAX_PRICE, description="Budget maximum en FCFA entiers (prix affiché)"),
]


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


class MessageChat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"] = Field(
        description="« user » : le client ; « assistant » : une réponse précédente du conseiller, "
        "renvoyée telle quelle par le frontend (historique)",
    )
    contenu: Annotated[str, AfterValidator(_content)] = Field(
        description=f"1 à {MAX_CONTENT_LENGTH} caractères après fusion des espaces",
    )


class DemandeConseil(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[MessageChat] = Field(
        min_length=1, max_length=MAX_MESSAGES,
        description=f"Historique tenu par le frontend, {MAX_MESSAGES} messages au plus, le dernier du client",
    )
    occasion: Annotated[str | None, AfterValidator(_context)] = Field(
        default=None, description=f"Occasion libre (mariage, cadeau…), {MAX_CONTEXT_LENGTH} caractères au plus",
    )
    style: Annotated[str | None, AfterValidator(_context)] = Field(
        default=None, description=f"Style libre (traditionnel, wax…), {MAX_CONTEXT_LENGTH} caractères au plus",
    )
    budget_max: Budget
    categories: list[Category] = Field(
        default_factory=list, max_length=MAX_CATEGORIES,
        description="Slugs ou ids de catégorie (sous-catégories comprises), comme `categorie` de la recherche",
    )

    @field_validator("messages")
    @classmethod
    def _last_message_from_client(cls, messages: list[MessageChat]) -> list[MessageChat]:
        if messages and messages[-1].role != "user":
            raise PydanticCustomError("dernier_message", LAST_MESSAGE_MESSAGE)
        return messages

    @field_validator("categories")
    @classmethod
    def _deduplicate(cls, categories: list[str]) -> list[str]:
        return _unique(categories)


class DemandeRecommandations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    categories: list[Category] = Field(
        default_factory=list, max_length=MAX_CATEGORIES,
        description="Slugs ou ids de catégorie ; vide : tout le catalogue",
    )
    budget_max: Budget

    @field_validator("categories")
    @classmethod
    def _deduplicate(cls, categories: list[str]) -> list[str]:
        return _unique(categories)


class ProduitConseille(ProduitRecherche):
    justification: str = Field(description="Pourquoi ce produit (300 caractères au plus, texte brut)")


class ReponseConseil(BaseModel):
    reponse: str = Field(description="Message du conseiller (texte brut, 600 caractères au plus)")
    produits_suggeres: list[ProduitConseille] = Field(description="4 au plus, produits visibles et en stock")
    conseils_style: list[str] = Field(description="3 au plus")
    source: Source = Field(description="« regles » : sélection par règles (simulé ou repli) ; « ia » : fournisseur d'IA")


class ReponseRecommandations(BaseModel):
    recommandations: list[ProduitConseille] = Field(description="8 au plus, produits visibles et en stock")
    source: Source
