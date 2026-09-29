"""Candidats du conseiller : produits du VRAI catalogue, lus par la recherche
du module 2 (app/services/search.py, vues publiques uniquement, rôle en
lecture seule, jamais en cache), sans la modifier.

Requêtes séquentielles (le pool a 10 connexions), 5 au plus, 20 lignes
chacune :
- un terme par requête (4 au plus), dans la catégorie demandée s'il n'y en
  a qu'une ; avec plusieurs catégories, les termes servent seulement au
  classement (la recherche ne filtre que sur une catégorie) ;
- s'il n'y a aucun résultat : une requête par catégorie demandée, sinon le
  catalogue entier (les plus récents).
Prix affiché au plus égal au budget (SQL), produits en rupture exclus,
dédoublonnés, 30 au plus.
"""
from app.services import search
from app.services.search import SearchFilters

MAX_CANDIDATS = 30


def _requetes(termes, categories, budget_max) -> tuple[list, list]:
    principales = []
    if termes and len(categories) <= 1:
        categorie = categories[0] if categories else None
        principales = [
            (SearchFilters(text=terme, category=categorie, max_price=budget_max), "pertinence") for terme in termes
        ]
    par_categorie = [(SearchFilters(category=categorie, max_price=budget_max), "date_desc") for categorie in categories]
    repli = par_categorie or [(SearchFilters(max_price=budget_max), "date_desc")]
    return principales, repli


async def rechercher(db, *, termes: tuple[str, ...], categories: list[str], budget_max: int | None) -> list:
    """Lignes de catalogue_produit_public (mêmes colonnes que la recherche),
    dans l'ordre de la recherche."""
    principales, repli = _requetes(termes, categories, budget_max)
    lignes: dict[int, object] = {}

    async def lire(requetes):
        for filters, sort in requetes:
            for row in await search.fetch_page(db, filters, sort, 1):
                if row["en_stock"]:
                    lignes.setdefault(row["id"], row)
            if len(lignes) >= MAX_CANDIDATS:
                return

    await lire(principales)
    if not lignes:
        await lire(repli)
    return list(lignes.values())[:MAX_CANDIDATS]
