from types import MappingProxyType
from typing import List
from app.modeles.conseiller_ia import (
    DemandeConseilIA,
    ReponseConseilIA,
    ProduitConseil,
    DemandeRecommandations,
    ReponseRecommandations,
)

# Maquette PROPRE au conseiller IA, en attendant le module 4 (qui lira les
# vues publiques du catalogue, comme la recherche). Reprise de l'ancienne
# maquette de la recherche (module 2), limitée aux champs lus ici.
# Immuable (tuple de dictionnaires en lecture seule) : aucun état partagé ni
# modifié entre requêtes. Aucun lien avec la recherche.
MOCK_PRODUCTS = tuple(
    MappingProxyType(product)
    for product in (
        {"id": 1, "nom": "Robe Baoulé Traditionnelle", "boutique_nom": "Atelier Baoulé Tiassalé",
         "prix": 25000.0, "categorie_nom": "Mode & Vêtements"},
        {"id": 2, "nom": "Chemise Wax Homme Motifs Éléphant", "boutique_nom": "Wax & Style Abidjan",
         "prix": 15000.0, "categorie_nom": "Mode & Vêtements"},
        {"id": 3, "nom": "Masque Baoulé en Bois d'Iroko", "boutique_nom": "Atelier Baoulé Tiassalé",
         "prix": 35000.0, "categorie_nom": "Artisanat & Déco"},
        {"id": 4, "nom": "Sac à Main Cuir et Pagne Kita", "boutique_nom": "Maroquinerie Bassam",
         "prix": 28000.0, "categorie_nom": "Maroquinerie & Accessoires"},
        {"id": 5, "nom": "Beurre de Karité Bio Brut 500g", "boutique_nom": "Cosmétiques du Nord",
         "prix": 4500.0, "categorie_nom": "Beauté & Bien-être"},
        {"id": 6, "nom": "Collier Perles Traditionnelles Akwaba", "boutique_nom": "Maroquinerie Bassam",
         "prix": 12000.0, "categorie_nom": "Bijoux & Parures"},
    )
)


class ServiceConseillerIA:
    """Assistant intelligent spécialisé dans la mode, l'artisanat et les traditions ivoiriennes."""

    @classmethod
    def generer_conseil(cls, demande: DemandeConseilIA) -> ReponseConseilIA:
        dernier_message = demande.messages[-1].contenu.lower() if demande.messages else ""
        style = (demande.style or "").lower()
        occasion = (demande.occasion or "").lower()
        budget = demande.budget_max

        produits_selectionnes: List[ProduitConseil] = []
        conseils: List[str] = []

        # Analyse des intentions et thématiques
        if any(w in dernier_message or w in occasion for w in ["mariage", "ceremonie", "fete", "dot", "chic"]):
            # Cérémonies traditionnelles ou chics : valorisation du pagne Baoulé et parures Akan
            for p in MOCK_PRODUCTS:
                if p["id"] in [1, 6]:  # Robe Baoulé, Collier Akan
                    if not budget or p["prix"] <= budget:
                        produits_selectionnes.append(
                            ProduitConseil(
                                id=p["id"],
                                nom=p["nom"],
                                boutique=p["boutique_nom"],
                                prix=p["prix"],
                                justification="Idéal pour les grandes cérémonies : pagne tissé Baoulé authentique et parures nobles Akan.",
                            )
                        )
            conseils.extend([
                "Pour une cérémonie traditionnelle ou un mariage, privilégiez le pagne Baoulé tissé ou le Kita noble.",
                "Associez une tenue unie avec une parure en bronze ou perles Akan pour un contraste raffiné.",
            ])

        elif any(w in dernier_message or w in style for w in ["homme", "chemise", "travail", "bureau", "wax"]):
            # Mode masculine / casual chic
            for p in MOCK_PRODUCTS:
                if p["id"] == 2:  # Chemise Wax Homme
                    if not budget or p["prix"] <= budget:
                        produits_selectionnes.append(
                            ProduitConseil(
                                id=p["id"],
                                nom=p["nom"],
                                boutique=p["boutique_nom"],
                                prix=p["prix"],
                                justification="Parfait pour allier confort moderne et identité africaine au bureau ou en soirée.",
                            )
                        )
            conseils.extend([
                "Une chemise en Wax cintrée se marie parfaitement avec un pantalon chino sombre ou un jean brut.",
            ])

        elif any(w in dernier_message or w in style for w in ["cadeau", "deco", "souvenir", "sculpture", "maison"]):
            # Décoration & Objets d'art
            for p in MOCK_PRODUCTS:
                if p["id"] in [3, 4]:  # Masque Iroko, Sac Kita
                    if not budget or p["prix"] <= budget:
                        produits_selectionnes.append(
                            ProduitConseil(
                                id=p["id"],
                                nom=p["nom"],
                                boutique=p["boutique_nom"],
                                prix=p["prix"],
                                justification="Objet artisanal de valeur avec passeport numérique d'authenticité certifié.",
                            )
                        )
            conseils.extend([
                "Les masques en bois d'Iroko apportent une touche prestigieuse et authentique à un intérieur contemporain.",
            ])

        elif any(w in dernier_message or w in style for w in ["beaute", "soin", "cheveux", "peau", "karite"]):
            for p in MOCK_PRODUCTS:
                if p["id"] == 5:
                    if not budget or p["prix"] <= budget:
                        produits_selectionnes.append(
                            ProduitConseil(
                                id=p["id"],
                                nom=p["nom"],
                                boutique=p["boutique_nom"],
                                prix=p["prix"],
                                justification="Soin 100% naturel et bio récolté par les coopératives féminines de Korhogo.",
                            )
                        )
            conseils.extend([
                "Appliquez le beurre de karité brut en massage le soir pour une hydratation intense de la peau et du cuir chevelu.",
            ])

        # Si aucun filtre spécifique n'a matché, sélection des coups de cœur selon le budget
        if not produits_selectionnes:
            for p in MOCK_PRODUCTS[:3]:
                if not budget or p["prix"] <= budget:
                    produits_selectionnes.append(
                        ProduitConseil(
                            id=p["id"],
                            nom=p["nom"],
                            boutique=p["boutique_nom"],
                            prix=p["prix"],
                            justification="Coup de cœur sélectionné par notre conseiller parmi nos artisans partenaires.",
                        )
                    )
            conseils.append("Explorez notre collection certifiée par passeport QR pour garantir l'origine ivoirienne de chaque pièce.")

        texte_reponse = (
            f"Bonjour ! En fonction de votre recherche, voici nos recommandations d'artisanat et créations de Côte d'Ivoire. "
            f"Nous avons sélectionné {len(produits_selectionnes)} article(s) confectionné(s) par nos artisans certifiés."
        )

        return ReponseConseilIA(
            reponse=texte_reponse,
            produits_suggeres=produits_selectionnes,
            conseils_style=conseils,
        )

    @classmethod
    def generer_recommandations(cls, demande: DemandeRecommandations) -> ReponseRecommandations:
        resultats: List[ProduitConseil] = []
        budget = demande.budget_max

        for p in MOCK_PRODUCTS:
            if budget and p["prix"] > budget:
                continue

            if demande.categories_preferees:
                if not any(cat.lower() in p["categorie_nom"].lower() for cat in demande.categories_preferees):
                    continue

            resultats.append(
                ProduitConseil(
                    id=p["id"],
                    nom=p["nom"],
                    boutique=p["boutique_nom"],
                    prix=p["prix"],
                    justification="Recommandé selon vos catégories favorites et votre budget.",
                )
            )

        return ReponseRecommandations(recommandations=resultats[:4])
