"""Contenu des données de démonstration (comptes, catalogue, adresse).

Tous les comptes de démo ont une adresse en @demo.anitche.test : c'est ce
domaine qui délimite les données supprimées par `seed_demo --reset`.
"""

DOMAINE_DEMO = 'demo.anitche.test'

# Mot de passe commun à tous les comptes de démo : public (docs/GUIDE_FRONTEND.md),
# donc réservé au développement (la commande refuse de tourner en production).
MOT_DE_PASSE_DEMO = 'Demo-Anitche-2026!'


def email_demo(identifiant):
    return f'{identifiant}@{DOMAINE_DEMO}'


ADMIN = {'email': email_demo('admin'), 'prenom': 'Adjoua', 'nom': 'Admin'}
SUPPORT = {'email': email_demo('support'), 'prenom': 'Serge', 'nom': 'Support'}
CLIENT = {'email': email_demo('client'), 'prenom': 'Awa', 'nom': 'Koné'}
LIVREUR = {'email': email_demo('livreur'), 'prenom': 'Kofi', 'nom': 'Yao'}

ADRESSE_LIVRAISON = {
    'commune': 'Cocody',
    'quartier': 'Angré 8e Tranche',
    'point_de_repere': 'Derrière la pharmacie des Oscars',
    'telephone': '+2250701020304',
}

# Point GPS de la même adresse (environ Angré, Cocody), donné au checkout d'une
# seule commande : celle en préparation, prochaine à livrer, pour tester le
# suivi du livreur et l'estimation d'arrivée. Les autres n'ont pas de point.
POSITION_LIVRAISON = {'latitude': 5.397340, 'longitude': -3.986620}

CATEGORIES = [
    {'nom': 'Mode', 'description': 'Vêtements, pagnes et accessoires.', 'ordre': 1},
    {'nom': 'Électronique', 'description': 'Téléphones, audio et accessoires.', 'ordre': 2},
    {'nom': 'Maison', 'description': 'Cuisine, décoration et linge de maison.', 'ordre': 3},
    {'nom': 'Beauté', 'description': 'Soins du corps, des cheveux et parfums.', 'ordre': 4},
]

# Une boutique par vendeur. Prix en FCFA entiers ; `stock` = quantité initiale
# de chaque variante. `couleur` : fond des images générées (Pillow).
BOUTIQUES = [
    {
        'cle': 'mode',
        'vendeur': {'email': email_demo('vendeur.mode'), 'prenom': 'Aminata', 'nom': 'Traoré'},
        'nom': 'Pagnes & Style',
        'description': 'Wax, bazin et prêt-à-porter cousus à Treichville.',
        'ville': 'Abidjan',
        'livraison_offerte': False,
        'couleur': (196, 69, 54),
        'categorie': 'Mode',
        'produits': [
            {'nom': 'Chemise en wax', 'description': 'Coupe ajustée, coton 100 %.',
             'variantes': [('Taille M', 12000, None, 15), ('Taille L', 12000, None, 12)]},
            {'nom': 'Robe en bazin brodé', 'description': 'Bazin riche, broderie main.',
             'variantes': [('Taille unique', 25000, 22000, 8)]},
            {'nom': 'Sac en pagne tissé', 'description': 'Anses en cuir, doublure intérieure.',
             'variantes': [('Modèle standard', 9500, None, 20)]},
        ],
    },
    {
        'cle': 'tech',
        'vendeur': {'email': email_demo('vendeur.tech'), 'prenom': 'Ibrahim', 'nom': 'Diallo'},
        'nom': 'Adjamé Tech',
        'description': 'Smartphones et accessoires garantis 6 mois.',
        'ville': 'Abidjan',
        'livraison_offerte': True,
        'couleur': (41, 98, 160),
        'categorie': 'Électronique',
        'produits': [
            {'nom': 'Smartphone Tecno Spark 20', 'description': '128 Go, double SIM.',
             'variantes': [('Noir 128 Go', 65000, None, 10), ('Bleu 128 Go', 65000, 62000, 6)]},
            {'nom': 'Écouteurs Bluetooth', 'description': 'Autonomie 24 h avec le boîtier.',
             'variantes': [('Blanc', 8000, None, 30)]},
            {'nom': 'Batterie externe 20 000 mAh', 'description': 'Deux ports USB, charge rapide.',
             'variantes': [('Modèle standard', 15000, None, 3)]},
        ],
    },
    {
        'cle': 'maison',
        'vendeur': {'email': email_demo('vendeur.maison'), 'prenom': 'Marie', 'nom': 'Kouassi'},
        'nom': 'Maison Akwaba',
        'description': 'Ustensiles de cuisine et décoration artisanale.',
        'ville': 'Bouaké',
        'livraison_offerte': False,
        'couleur': (46, 125, 90),
        'categorie': 'Maison',
        'produits': [
            {'nom': 'Marmite en fonte', 'description': 'Idéale pour la sauce graine.',
             'variantes': [('5 litres', 18000, None, 10), ('10 litres', 27000, None, 5)]},
            {'nom': 'Nappe en kita', 'description': 'Tissage Baoulé, 6 couverts.',
             'variantes': [('Modèle standard', 14000, None, 7)]},
            {'nom': 'Mortier et pilon', 'description': 'Bois massif sculpté.',
             'variantes': [('Grand modèle', 11000, None, 9)]},
        ],
    },
    {
        'cle': 'beaute',
        'vendeur': {'email': email_demo('vendeur.beaute'), 'prenom': 'Fatou', 'nom': 'Bamba'},
        'nom': 'Karité Doré',
        'description': 'Beurre de karité pur et soins naturels.',
        'ville': 'Korhogo',
        'livraison_offerte': False,
        'couleur': (184, 134, 11),
        'categorie': 'Beauté',
        'produits': [
            {'nom': 'Beurre de karité pur', 'description': 'Non raffiné, pressé à froid.',
             'variantes': [('250 g', 3500, None, 40), ('500 g', 6000, 5500, 25)]},
            {'nom': 'Savon noir', 'description': 'Au beurre de karité et à la cendre de cacao.',
             'variantes': [('Pain de 200 g', 1500, None, 60)]},
            {'nom': 'Huile de coco vierge', 'description': 'Flacon de 250 ml.',
             'variantes': [('250 ml', 4500, None, 4)]},
        ],
    },
]
