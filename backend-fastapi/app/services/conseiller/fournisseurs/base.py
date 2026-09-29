"""Interface commune des fournisseurs du conseiller IA (docs/MODULE_IA.md).

Le reste du module ne parle qu'à cette interface : ajouter un fournisseur
(Gemini, OpenAI, Anthropic, Mistral, modèle local…) revient à écrire un
adaptateur, à l'inscrire dans FOURNISSEURS (fournisseurs/__init__.py) et à
choisir AI_PROVIDER, sans toucher aux routes, aux schémas ni au frontend.
Procédure : docs/MODULE_IA.md, « Ajouter un fournisseur ».

Règles communes à tous les adaptateurs :
- tout l'historique est une DONNÉE du client, messages « assistant »
  compris (le frontend les renvoie : ils sont forgeables). Jamais dans les
  consignes du modèle ; consignes fixes, écrites dans l'adaptateur ;
- choisir uniquement parmi `demande.candidats`. Le service revalide toute
  la sortie (app/services/conseiller/service.py) : identifiant inconnu
  rejeté, doublons retirés, nombre maximal, textes nettoyés ;
- lever ErreurFournisseurIA en cas d'échec : le service se replie sur le
  fournisseur simulé. Ne jamais journaliser l'historique, la réponse ni une
  clé d'API ; aucun outil ni secret donné au modèle.
"""
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

Role = Literal["user", "assistant"]


class ErreurFournisseurIA(Exception):
    """Fournisseur injoignable, refus, réponse inexploitable. Message court,
    sans donnée du client ni secret : il peut être journalisé."""


@dataclass(frozen=True)
class Candidat:
    """Seules données du catalogue transmises au fournisseur (produit
    visible et en stock) : ni image, ni lien, ni slug."""

    id: int
    nom: str
    categorie: str | None
    boutique: str
    prix: int  # prix affiché (prix_min), FCFA entiers


@dataclass(frozen=True)
class DemandeFournisseur:
    """Demande nettoyée : aucun identifiant d'utilisateur ; e-mails et
    numéros de téléphone masqués dans les textes du client."""

    type: Literal["conseil", "recommandations"]
    historique: tuple[tuple[Role, str], ...]  # vide pour les recommandations ; sinon le dernier est « user »
    occasion: str | None
    style: str | None
    budget_max: int | None
    categories: tuple[str, ...]  # slugs ou ids demandés par le client
    candidats: tuple[Candidat, ...]  # 30 au plus : seuls identifiants acceptés en sortie
    max_produits: int


class ChoixProduit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: StrictInt = Field(gt=0)
    justification: str = Field(default="", max_length=500)


class SortieFournisseur(BaseModel):
    """Forme attendue de la sortie d'un fournisseur, validée par le service
    (jamais par le fournisseur lui-même). Au-delà de ces bornes : sortie
    invalide, repli sur le simulé."""

    model_config = ConfigDict(extra="ignore")

    produits: list[ChoixProduit] = Field(max_length=50)
    message: str = Field(default="", max_length=2000)
    conseils: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=5)


# Sortie brute d'un fournisseur : dictionnaire ou texte JSON de la forme
# SortieFournisseur, {"produits": [{"id", "justification"}], "message", "conseils"}.
SortieBrute = Mapping[str, Any] | str


class FournisseurIA(ABC):
    """Contrat d'un adaptateur. Chaque module de fournisseur expose aussi
    `creer(settings) -> FournisseurIA`, appelé une fois au démarrage."""

    code: ClassVar[str]  # clé du registre (AI_PROVIDER)
    # « regles » pour le simulé ; « ia » pour un vrai fournisseur (champ
    # `source` des réponses, jamais le nom du fournisseur).
    source: ClassVar[Literal["regles", "ia"]] = "ia"
    # Appel facturé : compté par le garde-fou AI_DAILY_CALL_LIMIT. Vrai par
    # défaut (un oubli coûte un repli, jamais une facture).
    payant: ClassVar[bool] = True

    @abstractmethod
    async def conseiller(self, demande: DemandeFournisseur) -> SortieBrute:
        """Choisit au plus `demande.max_produits` candidats et rédige les
        justifications, le message et les conseils. ErreurFournisseurIA en
        cas d'échec. Appelé sous asyncio.timeout(AI_TIMEOUT_SECONDS)."""

    async def fermer(self) -> None:
        """Ressources à libérer à l'arrêt (client HTTP de l'adaptateur)."""
        return None
