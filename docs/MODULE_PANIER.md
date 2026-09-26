# Module panier — contrat et règles

> Périmètre : backend Django, `backend-django/apps/panier/`.
> Document vivant : à corriger dès qu'une règle est arbitrée en réunion.

## 1. Ce sur quoi le module s'appuie

| Élément | Où | Rôle |
|---|---|---|
| `VarianteProduit` (`est_active`, `prix_effectif`, `stock`) | `apps/catalogue/models.py` | Ce qu'on met au panier : une variante, jamais un produit |
| `Produit.est_achetable` | idem | `produit.est_actif` **et** `boutique.est_publiable` |
| `Boutique.est_publiable` | `apps/vendeurs/models.py` | Boutique ouverte, non suspendue, vendeur validé et actif |
| `Stock.est_en_stock()` | `apps/catalogue/models.py` | Contrôle de quantité à l'ajout et à la modification |

Le panier **ne réserve pas de stock** : il vérifie la disponibilité au moment de l'ajout. La vérification qui fait foi a lieu au checkout (`commandes.ValiderPanierView`), sous verrou des lignes de stock. Le checkout exige une adresse de livraison et un email vérifié : voir `MODULE_COMMANDES.md`.

## 2. Modèle

- `Panier` : appartient **soit** à un utilisateur (`utilisateur`), **soit** à un visiteur anonyme (`session_key`, clé de la session Django).
  - **Un seul panier par compte et un seul par session** (contraintes uniques conditionnelles `panier_unique_par_utilisateur`, `panier_unique_par_session`).
  - `total` : somme des sous-totaux des **seules lignes disponibles**.
  - `nombre_articles` : somme des quantités, lignes indisponibles comprises.
  - `lignes()` : lignes du panier (profite d'un `prefetch_related`) ; liste vide pour un panier non enregistré.
- `PanierItem` : une ligne = une variante + une quantité.
  - **Une seule ligne par variante et par panier** (`panier_item_unique_par_variante`) : un nouvel ajout de la même variante incrémente la ligne existante.
  - **Quantité ≥ 1**, garantie en base (`panier_item_quantite_positive`) et par l'API.
  - `motif_indisponibilite` / `est_disponible` : voir § 4.
  - `PanierItem.objects.avec_details()` : `select_related` de tout ce que la sérialisation lit (variante, stock, produit, boutique, propriétaire). À utiliser pour toute lecture de lignes.

Logique métier : `apps/panier/services.py` (`get_panier_existant`, `get_or_create_panier`, `ajouter_article`, `modifier_quantite`). Le checkout importe ses fonctions depuis ce fichier, jamais depuis les vues.

## 3. Endpoints — `/api/panier/`

Tous en `AllowAny` : le panier courant est déterminé par le JWT (utilisateur connecté) ou, à défaut, par le cookie de session (visiteur anonyme). On n'accède jamais au panier d'un autre : une ligne d'un autre panier renvoie **404**.

| Méthode | URL | Description |
|---|---|---|
| GET | `panier/` | Panier courant avec ses lignes, `total`, `nombre_articles`. Sans panier existant : panier vide avec `id: null` (rien n'est créé en base) |
| GET | `panier/items/` | Lignes du panier courant (paginées) |
| POST | `panier/items/` | Corps `{"variante": <id>, "quantite": n}` (`quantite` ≥ 1, défaut 1). Crée le panier au premier ajout. Si la variante est déjà au panier, incrémente la ligne. Réponse **201** dans les deux cas |
| GET | `panier/items/<uuid>/` | Une ligne |
| PATCH / PUT | `panier/items/<uuid>/` | Seule `quantite` est modifiable (≥ 1) |
| DELETE | `panier/items/<uuid>/` | Retire la ligne (**204**). Seul moyen de retirer un article : une quantité 0 est refusée |

### Refus (400)
- Ajout d'un article **indisponible** (variante inactive, produit inactif, boutique fermée/suspendue, vendeur non validé) → `errors.variante` : « Cet article n'est plus disponible à la vente. … ».
- `quantite` < 1 → `errors.quantite`.
- Quantité (cumulée, à l'ajout) supérieure au stock → « Stock insuffisant : X disponible(s), Y demandé(s). »
- **Changer la variante d'une ligne** (PATCH/PUT avec une autre `variante`) → `errors.variante` : « La variante d'une ligne de panier ne peut pas être modifiée. Supprimez la ligne puis ajoutez la nouvelle variante. » Renvoyer la même variante (PUT complet) est accepté.

### Représentation
- **Ligne** : `id`, `panier`, `variante`, `variante_detail`, `quantite`, `prix_unitaire`, `sous_total`, `est_disponible`, `motif_indisponibilite`, `added_at`.
- `variante_detail.stock` ne contient que `{"est_en_stock": bool}` : ni `quantite_disponible` ni `seuil_alerte` (donnée interne du vendeur) ne sont exposés dans le panier.
- **Panier** : `id` (null si non enregistré), `utilisateur`, `items`, `total`, `nombre_articles`, `created_at`, `updated_at`. `session_key` n'est **jamais** renvoyé : c'est la valeur du cookie de session, que le JSON rendrait lisible par un script (contournement de HttpOnly).

## 4. Article devenu indisponible après coup

Une ligne ajoutée alors que l'article était disponible peut le devenir ensuite (boutique suspendue ou fermée, vendeur qui perd sa validation, produit ou variante désactivés). Règle retenue :

- La ligne **reste dans le panier**, marquée `est_disponible: false` avec un `motif_indisponibilite` (volontairement générique côté boutique : le client ne sait pas si elle est fermée ou suspendue).
- Elle est **exclue du `total`**.
- Le client peut la **supprimer** ou **baisser** sa quantité ; toute **hausse** est refusée (400).
- Si l'article redevient disponible (suspension levée…), la ligne redevient normale sans action du client.
- **Checkout** : la commande entière est refusée tant qu'il reste une ligne indisponible, et le message liste **toutes** ces lignes (« Produit (Variante) »), pour que le client les retire en une fois.

Même règle partout : `PanierItem.est_disponible` (variante active et `produit.est_achetable`) est utilisé par l'API panier et par le checkout.

Contrôle en modification de quantité : une **baisse** est toujours autorisée, sans contrôle de stock ni de disponibilité (elle rapproche le panier d'un état valide ; le checkout revérifie le stock sous verrou). Seule une **hausse** est contrôlée : l'article doit être disponible à la vente, puis le stock doit couvrir la nouvelle quantité.

## 5. Concurrence

- Deux premiers ajouts simultanés ne créent qu'un panier (contraintes uniques ; `get_or_create` relit le panier créé par l'autre requête).
- Les ajouts d'un même panier sont sérialisés par un verrou sur la ligne `Panier` (`select_for_update`) : pas de ligne en double, quantité cumulée vérifiée sur une valeur à jour.
- La modification de quantité verrouille la ligne concernée.

## 6. Dépendances avec les autres modules

- **commandes** : `ValiderPanierView` utilise `get_or_create_panier` (`apps/panier/services.py`), `PanierItem.objects.avec_details()` et `PanierItem.est_disponible`, puis vide le panier après création des commandes.
- **catalogue / vendeurs** : toute évolution de `Produit.est_achetable` ou `Boutique.est_publiable` s'applique automatiquement au panier.

## 7. Décisions produit en attente

1. **Panier anonyme en production.** L'API s'authentifie par JWT et `CORS_ALLOW_CREDENTIALS = False` en prod. Si le frontend est servi depuis une autre origine que l'API, le navigateur n'enverra pas le cookie de session : le panier anonyme ne fonctionnerait alors pas (hypothèse non vérifiée en conditions réelles). Options : même origine (reverse proxy), autoriser les credentials CORS pour l'origine du frontend, ou un identifiant de panier anonyme porté autrement.
2. **Fusion du panier anonyme à la connexion.** Aucune fusion n'existe : un visiteur qui se connecte retrouve le panier de son compte, son panier anonyme n'est pas repris. Règle à définir (fusion des quantités ? plafond au stock ? lignes indisponibles ?).

## 8. Dette connue

- **Nettoyage des paniers abandonnés** (niveau 2 — production) : les paniers anonymes, et les paniers orphelins d'un compte supprimé (`utilisateur` passe à NULL), ne sont jamais purgés. À traiter par une tâche périodique (Celery beat) quand le volume le justifiera.
- Le message « Stock insuffisant » indique la quantité disponible exacte (comportement existant, conservé).

## 9. Tests

- `backend-django/apps/panier/tests.py` — 30 tests : création paresseuse du panier, isolation entre paniers, ajout et incrément, refus d'un article indisponible (4 cas), ligne devenue indisponible (marquage, total, baisse/suppression/hausse, retour à la normale), quantités (0, négative, > stock, baisse toujours permise / hausse contrôlée, contrainte en base), variante figée, `session_key` non exposée, contraintes d'unicité, nombre de requêtes constant (`panier/` et `panier/items/`), panier vide `id: null`, stock non détaillé, concurrence (8 premiers ajouts simultanés → un panier, une ligne, aucun 500).
- `backend-django/apps/commandes/tests.py` : `test_articles_indisponibles_tous_listes_et_validation_refusee`.

⚠️ **Toujours lancer les tests sur PostgreSQL.** Sans `DJANGO_SETTINGS_MODULE`, `manage.py test` bascule sur SQLite et `PanierConcurrenceTestCase` est **sauté silencieusement**.

Depuis la machine hôte (Postgres de `infra/docker-compose.yml` démarré, port 5432 exposé) :

```bash
cd backend-django
DJANGO_SETTINGS_MODULE=config.settings.ci DB_NAME=anitche_test DB_USER=postgres DB_PASSWORD=postgres DB_HOST=localhost \
  python manage.py test apps.panier apps.commandes -v 2
```

Vérifier dans la sortie `-v 2` que `test_premiers_ajouts_simultanes_un_seul_panier_une_seule_ligne` affiche `ok` et non `skipped`.

## 10. Migration `0002_contraintes_unicite_et_quantite`

Avant d'ajouter les contraintes, la migration vérifie les données existantes : paniers en double (par utilisateur ou par session), lignes en double (même panier, même variante), lignes de quantité 0. Si elle en trouve, elle **s'arrête et liste les ids** ; elle ne fusionne ni ne supprime rien. Corriger les données puis relancer `migrate`.
