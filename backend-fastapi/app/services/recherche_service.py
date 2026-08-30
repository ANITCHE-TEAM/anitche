import math
from typing import List, Optional, Dict, Any
from app.modeles.recherche import (
    ProduitSearchResult,
    ReponseRecherche,
    FacettesRecherche,
    SuggestionItem,
    ReponseSuggestions,
)

# Base de connaissances / index de recherche initial pour l'artisanat et les produits ivoiriens
CATALOGUE_INDEX: List[Dict[str, Any]] = [
    {
        "id": 1,
        "nom": "Robe Baoulé Traditionnelle",
        "slug": "robe-baoule-traditionnelle",
        "boutique_id": 1,
        "boutique_nom": "Atelier Baoulé Tiassalé",
        "prix": 25000.0,
        "categorie_nom": "Mode & Vêtements",
        "note_moyenne": 4.9,
        "disponible": True,
        "description": "Véritable pagne tissé Baoulé cousu main avec finitions modernes pour cérémonies et sorties chic.",
        "tags": ["baoule", "pagne", "robe", "traditionnel", "tissu", "femme", "mariage"],
    },
    {
        "id": 2,
        "nom": "Chemise Wax Homme Motifs Éléphant",
        "slug": "chemise-wax-homme-motifs-elephant",
        "boutique_id": 2,
        "boutique_nom": "Wax & Style Abidjan",
        "prix": 15000.0,
        "categorie_nom": "Mode & Vêtements",
        "note_moyenne": 4.7,
        "disponible": True,
        "description": "Chemise coupe cintrée en pur coton Wax hollandais haut de gamme avec motifs inspirés de la Côte d'Ivoire.",
        "tags": ["wax", "chemise", "homme", "coton", "africain", "mode", "casual"],
    },
    {
        "id": 3,
        "nom": "Masque Baoulé en Bois d'Iroko",
        "slug": "masque-baoule-bois-iroko",
        "boutique_id": 1,
        "boutique_nom": "Atelier Baoulé Tiassalé",
        "prix": 35000.0,
        "categorie_nom": "Artisanat & Déco",
        "note_moyenne": 5.0,
        "disponible": True,
        "description": "Masque sculptural sculpté à la main dans du bois d'Iroko noble par les artisans sculpteurs de Tiassalé.",
        "tags": ["masque", "baoule", "artisanat", "bois", "iroko", "deco", "sculpture", "authentique"],
    },
    {
        "id": 4,
        "nom": "Sac à Main Cuir et Pagne Kita",
        "slug": "sac-main-cuir-pagne-kita",
        "boutique_id": 3,
        "boutique_nom": "Maroquinerie Bassam",
        "prix": 28000.0,
        "categorie_nom": "Maroquinerie & Accessoires",
        "note_moyenne": 4.8,
        "disponible": True,
        "description": "Sacoche élégante en cuir tanné végétal rehaussée d'empiècements en véritable tissu Kita tissé.",
        "tags": ["sac", "cuir", "kita", "maroquinerie", "accessoire", "femme", "bassam"],
    },
    {
        "id": 5,
        "nom": "Beurre de Karité Bio Brut 500g",
        "slug": "beurre-karite-bio-brut-500g",
        "boutique_id": 4,
        "boutique_nom": "Cosmétiques du Nord",
        "prix": 4500.0,
        "categorie_nom": "Beauté & Bien-être",
        "note_moyenne": 4.9,
        "disponible": True,
        "description": "Beurre de karité 100% pur non raffiné extrait traditionnellement à Korhogo pour peaux et cheveux.",
        "tags": ["karite", "bio", "naturel", "beaute", "soin", "korhogo", "beurre"],
    },
    {
        "id": 6,
        "nom": "Collier Perles Traditionnelles Akwaba",
        "slug": "collier-perles-traditionnelles-akwaba",
        "boutique_id": 3,
        "boutique_nom": "Maroquinerie Bassam",
        "prix": 12000.0,
        "categorie_nom": "Bijoux & Parures",
        "note_moyenne": 4.6,
        "disponible": True,
        "description": "Parure ornée de perles de verre et de bronze coulé selon la méthode traditionnelle Akan.",
        "tags": ["collier", "bijoux", "perles", "akwaba", "bronze", "akan", "parure"],
    },
]


class ServiceRecherche:
    """Moteur de recherche rapide et filtrage de catalogue pour ANITCHE."""

    @classmethod
    def rechercher_produits(
        cls,
        q: Optional[str] = None,
        categorie: Optional[str] = None,
        prix_min: Optional[float] = None,
        prix_max: Optional[float] = None,
        boutique_id: Optional[int] = None,
        tri: Optional[str] = "pertinence",
        page: int = 1,
        par_page: int = 10,
    ) -> ReponseRecherche:
        resultats_bruts = list(CATALOGUE_INDEX)

        # 1. Filtre textuel
        if q and q.strip():
            mots_cles = q.strip().lower().split()
            filtres = []
            for p in resultats_bruts:
                texte_recherche = f"{p['nom']} {p['description']} {p['boutique_nom']} {' '.join(p['tags'])}".lower()
                score = sum(1 for mot in mots_cles if mot in texte_recherche)
                if score > 0:
                    p_avec_score = dict(p)
                    p_avec_score["_score"] = score
                    filtres.append(p_avec_score)
            resultats_bruts = filtres
        else:
            for p in resultats_bruts:
                p["_score"] = 1

        # 2. Filtre catégorie
        if categorie:
            cat_clean = categorie.strip().lower()
            resultats_bruts = [p for p in resultats_bruts if cat_clean in p["categorie_nom"].lower()]

        # 3. Filtre prix
        if prix_min is not None:
            resultats_bruts = [p for p in resultats_bruts if p["prix"] >= prix_min]
        if prix_max is not None:
            resultats_bruts = [p for p in resultats_bruts if p["prix"] <= prix_max]

        # 4. Filtre boutique
        if boutique_id is not None:
            resultats_bruts = [p for p in resultats_bruts if p["boutique_id"] == boutique_id]

        # 5. Tri
        if tri == "prix_asc":
            resultats_bruts.sort(key=lambda x: x["prix"])
        elif tri == "prix_desc":
            resultats_bruts.sort(key=lambda x: x["prix"], reverse=True)
        elif tri == "note":
            resultats_bruts.sort(key=lambda x: x["note_moyenne"], reverse=True)
        else:
            resultats_bruts.sort(key=lambda x: x.get("_score", 1), reverse=True)

        # 6. Facettes
        categories_compteur: Dict[str, int] = {}
        boutiques_compteur: Dict[str, Dict[str, Any]] = {}
        prix_list = [p["prix"] for p in CATALOGUE_INDEX]

        for p in CATALOGUE_INDEX:
            cat = p["categorie_nom"]
            categories_compteur[cat] = categories_compteur.get(cat, 0) + 1

            b_nom = p["boutique_nom"]
            b_id = p["boutique_id"]
            if b_nom not in boutiques_compteur:
                boutiques_compteur[b_nom] = {"id": b_id, "nom": b_nom, "total": 0}
            boutiques_compteur[b_nom]["total"] += 1

        facettes = FacettesRecherche(
            categories=[{"nom": k, "nombre": v} for k, v in categories_compteur.items()],
            fourchettes_prix={"min": min(prix_list) if prix_list else 0.0, "max": max(prix_list) if prix_list else 100000.0},
            boutiques=list(boutiques_compteur.values()),
        )

        # 7. Pagination
        total = len(resultats_bruts)
        pages_total = math.ceil(total / par_page) if total > 0 else 1
        page_valide = max(1, min(page, pages_total))
        debut = (page_valide - 1) * par_page
        fin = debut + par_page

        produits_page = [
            ProduitSearchResult(
                id=p["id"],
                nom=p["nom"],
                slug=p["slug"],
                boutique_id=p["boutique_id"],
                boutique_nom=p["boutique_nom"],
                prix=p["prix"],
                image_url=p.get("image_url"),
                categorie_nom=p["categorie_nom"],
                note_moyenne=p["note_moyenne"],
                disponible=p["disponible"],
                description=p.get("description", ""),
            )
            for p in resultats_bruts[debut:fin]
        ]

        return ReponseRecherche(
            total=total,
            page=page_valide,
            par_page=par_page,
            pages_total=pages_total,
            resultats=produits_page,
            facettes=facettes,
        )

    @classmethod
    def suggerer(cls, q: str, limite: int = 5) -> ReponseSuggestions:
        if not q or len(q.strip()) < 2:
            return ReponseSuggestions(requete=q, suggestions=[])

        terme = q.strip().lower()
        suggestions: List[SuggestionItem] = []

        # 1. Correspondances dans les noms de produits
        for p in CATALOGUE_INDEX:
            if terme in p["nom"].lower():
                suggestions.append(SuggestionItem(texte=p["nom"], type="produit", id=p["id"], score=1.0))

        # 2. Correspondances dans les catégories
        categories_vues = set()
        for p in CATALOGUE_INDEX:
            cat = p["categorie_nom"]
            if terme in cat.lower() and cat not in categories_vues:
                categories_vues.add(cat)
                suggestions.append(SuggestionItem(texte=cat, type="categorie", score=0.8))

        # 3. Correspondances dans les tags artisanat / tissus
        tags_vus = set()
        for p in CATALOGUE_INDEX:
            for t in p.get("tags", []):
                if terme in t and t not in tags_vus:
                    tags_vus.add(t)
                    suggestions.append(SuggestionItem(texte=f"Tissu & Style : {t.capitalize()}", type="artisanat", score=0.6))

        return ReponseSuggestions(requete=q, suggestions=suggestions[:limite])
