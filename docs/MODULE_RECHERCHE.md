# Module recherche — contrat et règles

> Périmètre : backend FastAPI, `backend-fastapi/app/` (routeur `routeurs/recherche.py`, service `services/search.py`, cache `core/cache.py`, modèles `modeles/recherche.py`).
> État : refonte de septembre 2026 (module 2 de la refonte FastAPI). Les règles de visibilité du catalogue restent celles de Django ([`MODULE_CATALOGUE.md`](./MODULE_CATALOGUE.md) § 2) : FastAPI **lit** des vues SQL publiques créées par une migration Django, ne touche à aucune table du catalogue et n'écrit rien.

## 1. Ce sur quoi le module s'appuie

| Dépendance | Usage |
|---|---|
| Vues SQL de la migration Django `catalogue/0004_recherche_publique` | `catalogue_produit_public` (produits visibles, prix affiché, `en_stock`, image principale, textes normalisés), `catalogue_categorie_publique` (catégories actives), `catalogue_boutique_publique` (boutiques publiques). Protégées côté Django par un **test de parité** (`CatalogueVuesPubliquesTests`) |
| Fonction `catalogue_normaliser(texte)` et extensions `unaccent`, `pg_trgm` (même migration) | Minuscules sans accents, trigrammes ; index GIN sur le nom et sur nom + description, index `(date_creation DESC, id DESC)` |
| PostgreSQL, rôle `anitche_fastapi_ro` | `SELECT` sur les **3 vues seulement** (`infra/postgres/fastapi_readonly.sql`) |
| Redis (base 2) | Cache de 60 s des totaux, facettes et suggestions ; limites de débit du socle |

Aucune authentification : routes publiques, comme la liste Django `GET /api/catalogue/produits/`.

## 2. Architecture

```text
routeurs/recherche.py   paramètres (noms de Django), validation (400), pagination, liens next/previous
services/search.py      construction du SQL paramétré (vues seulement), exécution, mise en forme
core/cache.py           cache JSON Redis avec durée de vie, facultatif (panne : calcul direct)
modeles/recherche.py    schémas de réponse (OpenAPI)
```

Plus aucune donnée en dur ni état global : l'ancienne maquette `CATALOGUE_INDEX` (6 produits inventés, modifiée à chaque recherche) est supprimée. Le conseiller IA (module 4, [`MODULE_IA.md`](./MODULE_IA.md)) n'a plus de maquette non plus : il choisit ses produits parmi ceux que renvoie `fetch_page` (mêmes vues, mêmes règles), sans modifier ce service.

## 3. Contrat HTTP

Base : `http://localhost:8001` en dev, `https://anitche.com/fast` en prod ([`GUIDE_FRONTEND.md`](./GUIDE_FRONTEND.md) § 12).

### `GET /recherche/produits`

Paramètres : mêmes noms et même sens que la liste Django. Une valeur invalide reçoit un **400** (Django l'ignore en silence). Paramètre inconnu (dont les anciens `q`, `boutique_id`, `par_page`) : ignoré.

| Paramètre | Valeurs | Règle |
|---|---|---|
| `recherche` | 2 à 100 caractères (espaces fusionnés) | Vide : pas de recherche (tout le catalogue). Caractères de contrôle refusés. 8 mots au plus utilisés, mots d'un caractère ignorés (§ 4) |
| `categorie` | slug ou id, 120 caractères au plus | Catégorie **et ses sous-catégories** (un niveau). Slug numérique (`2024`) : lu comme slug **et** comme id. Lettres, chiffres, `_`, `-` |
| `boutique` | id (chiffres) ou slug, 140 caractères au plus | Comme Django |
| `prix_min`, `prix_max` | entiers FCFA, 0 à 9 999 999 999 | Sur le **prix affiché** ; `prix_min > prix_max` → 400 sur `prix_max` |
| `tri` | `pertinence`, `date_desc`, `date_asc`, `prix_asc`, `prix_desc` | Défaut : `pertinence` avec `recherche`, `date_desc` sans. `note` supprimé (aucun module d'avis) |
| `page` | 1 à 50 | 20 résultats par page (comme Django), 1 000 au plus |

Réponse : **même enveloppe et mêmes champs que la liste Django**, plus `facettes` :

```json
{
  "count": 3,
  "next": null,
  "previous": null,
  "results": [
    {
      "id": 41, "nom": "Beurre de karité pur", "slug": "beurre-de-karite-pur-3d85cf",
      "prix_base": "3500.00", "prix_min": 3500.0,
      "image_principale": "http://localhost:8000/media/catalogue/produits/2026/09/produit_GJzjrY5.png",
      "categorie": 8, "categorie_nom": "Beauté",
      "boutique": 14, "boutique_nom": "Karité Doré", "boutique_slug": "karite-dore",
      "en_stock": true, "date_creation": "2026-09-27T20:27:03.031043Z"
    }
  ],
  "facettes": {
    "categories": [{"id": 8, "nom": "Beauté", "slug": "beaute", "parent": null, "nombre": 3}],
    "boutiques": [{"id": 14, "nom": "Karité Doré", "slug": "karite-dore", "nombre": 3}],
    "prix": {"min": 1500.0, "max": 4500.0, "tranches": [{"min": 0, "max": 5000, "nombre": 3}]}
  }
}
```

- Champs d'un produit identiques à `ProduitPublicListSerializer` (vérifié sur la démo : 18 produits, 0 écart de valeur). Seule différence : produit **sans catégorie** → `categorie_nom: null` (Django **omet** la clé).
- **Jamais de stock exact** : seulement `en_stock` (au moins une variante active en stock). Ni description, ni SKU, ni seuil d'alerte.
- `image_principale` : `MEDIA_BASE_URL` + chemin de l'image, encodé comme Django ; `null` sans image.
- `next` / `previous` : URL absolues construites avec `PUBLIC_BASE_URL` (jamais l'en-tête `Host`), filtres validés conservés, triés ; `previous` de la page 2 sans `page` (comme DRF).
- `count` et `facettes` peuvent avoir **60 s de retard** (cache, § 7) ; `results` jamais.
- `facettes` : en **page 1 seulement** (`null` ensuite).

### `GET /recherche/suggestions`

| Paramètre | Valeurs |
|---|---|
| `recherche` | obligatoire, 3 à 50 caractères (2 : l'index trigramme ne sert à rien, mesuré) |
| `limite` | 1 à 10, défaut 8 |

```json
{"requete": "karit", "suggestions": [
  {"type": "boutique", "texte": "Karité Doré", "id": 14, "slug": "karite-dore"},
  {"type": "produit", "texte": "Beurre de karité pur", "id": 41, "slug": "beurre-de-karite-pur-3d85cf"}
]}
```

- Sources : **catégories actives** (2 au plus), **boutiques publiques** (2 au plus), **produits visibles**. Rien de ce que Django masque.
- Ordre : le nom commence par le texte, puis un mot du nom commence par le texte, puis le nom contient le texte, puis nom approchant (faute de frappe) ; à égalité catégorie, boutique, produit.
- `slug` pour naviguer (Django) : produit `GET /api/catalogue/produits/<slug>/`, catégorie `GET /api/catalogue/categories/<slug>/` (ou liste FastAPI `?categorie=<slug>`), boutique `GET /api/vendeurs/boutiques/<slug>/`.
- En cache 60 s (clé : texte exact et limite) : un produit masqué dans Django peut rester **suggéré** jusqu'à 60 s ; sa fiche Django répond alors 404.

### Réponses d'erreur (format commun, [`GUIDE_FRONTEND.md`](./GUIDE_FRONTEND.md) § 5)

| Code | Cas |
|---|---|
| 400 | Paramètre invalide, clé = nom du paramètre (`errors.recherche`, `errors.prix_max`…) |
| 404 `page_invalide` | Page au-delà de la dernière (texte de Django : « Page non valide. ») |
| 429 | Limite de débit (`Retry-After`) : `search` 1 200/h et `suggestions` 2 400/h par IP |
| 503 | PostgreSQL ou Redis indisponible, requête trop longue (3 s) : repli sur la liste Django (§ 10) |

## 4. Règle de recherche

Texte cherché : espaces fusionnés, composition Unicode canonique (NFC). Minuscules et accents : **uniquement** dans PostgreSQL, avec `catalogue_normaliser` (la fonction des index ; une normalisation Python n'aurait pas les mêmes règles et les index ne serviraient plus).

Un produit correspond si :

1. **chaque mot** (2 caractères au moins, 8 au plus) est contenu dans son nom ou sa description ;
2. **ou** le texte ressemble à son nom : similarité de mots `pg_trgm` (`<%`, seuil par défaut 0,6), qui rattrape fautes de frappe et pluriels (`chemize` → « Chemise en wax », `karitee` → « Beurre de karité pur ») ;
3. **ou** chaque mot est contenu dans le nom de sa **boutique**, de sa **catégorie** ou de la catégorie parente.

Pertinence (`tri=pertinence`), par paliers : 1 tous les mots dans le nom, 2 nom approchant, 3 nom et description, 4 boutique ou catégorie ; puis similarité au nom, date décroissante, id décroissant.

Comportement connu : le critère 2 porte sur tout le texte. Pour une recherche de plusieurs mots, un nom qui contient l'un des mots peut atteindre le seuil (« robe rouge » peut trouver « Chemise rouge », au palier 2, après les correspondances exactes). Resserrer (similarité mot par mot) est possible ; à décider sur retour d'usage.

**Jokers de `LIKE` échappés** (`\`, puis `%` et `_`) **après** la normalisation, dans le SQL : `unaccent` transforme `％`, `＿`, `＼` (pleine chasse) en `%`, `_`, `\`. Un échappement fait en Python avant la normalisation laisserait passer ces jokers (vérifié par un test de mutation). Sans échappement, `__` ou `%%` renverraient tout le catalogue ; avec, rien.

Toute valeur du client est un **paramètre** (`$n`). Le texte SQL ne contient que des fragments écrits dans `services/search.py` (un par mot, un par filtre) : il est identique pour deux valeurs de même forme (testé).

## 5. Tri et pagination

| `tri` | Ordre SQL |
|---|---|
| `pertinence` | palier, similarité au nom ↓, date ↓, id ↓ |
| `date_desc` | `date_creation DESC, id DESC` (index) |
| `date_asc` | `date_creation, id` |
| `prix_asc` / `prix_desc` | prix affiché, puis date ↓, id ↓ |

L'id en dernier rend la pagination stable (Django s'arrête à la date). 20 par page, page 50 au plus : borne le coût de l'`OFFSET`. Page au-delà de la dernière (d'après `count`) : 404.

## 6. Facettes

Calculées par PostgreSQL sur **tout l'ensemble filtré** (recherche et filtres), en une requête qui donne aussi `count` :

| Facette | Contenu |
|---|---|
| `categories` | catégories **directes** des résultats `{id, nom, slug, parent, nombre}`, 20 au plus, par nombre décroissant |
| `boutiques` | `{id, nom, slug, nombre}`, 10 au plus |
| `prix` | `min`, `max` du prix affiché ; `tranches` non vides : 0-5 000, 5 000-10 000, 10 000-25 000, 25 000-50 000, 50 000-100 000, 100 000 et plus (min inclus, max exclu, dernière `max: null`) |

Pour filtrer sur une tranche : `prix_min=<min>&prix_max=<max>` (bornes incluses côté filtre). Facettes « disjonctives » (chaque facette sans son propre filtre) : non retenues, plus coûteuses ; à revoir sur demande du frontend.

## 7. Cache Redis (60 s)

| Clé (base 2) | Contenu | Quand |
|---|---|---|
| `fastapi:cache:recherche:facettes:v1:<sha256>` | `count` et facettes | page 1 |
| `fastapi:cache:recherche:total:v1:<sha256>` | `count` seul (repris des facettes s'il y en a) | pages suivantes |
| `fastapi:cache:recherche:suggestions:v1:<sha256>` | suggestions | chaque appel |

- Empreinte des filtres validés (jamais le texte en clair dans la clé) ; le tri et la page n'en font pas partie.
- **Jamais de cache sur `results`** : un produit masqué dans Django disparaît dès la requête suivante (testé sur les vraies vues).
- Redis indisponible pendant la lecture ou l'écriture du cache : calcul direct, incident journalisé (comme le cache d'authentification). Redis complètement en panne : la limite de débit répond déjà **503** (socle).
- `SEARCH_CACHE_TTL` (0 à 300, 0 : pas de cache).

## 8. PostgreSQL : droits et plans

Droits (`infra/postgres/fastapi_readonly.sql`, relancé après `migrate`) : `REVOKE ALL` puis `GRANT SELECT` sur les 3 vues. **Aucune table du catalogue ni des boutiques** n'est lisible (testé sur toutes les tables `catalogue_*` et `vendeurs_*`). Les vues s'exécutent avec les droits de leur propriétaire (compte des migrations). Un retour arrière de la migration 0004 supprime les vues et ces droits.

Plans : asyncpg prépare les requêtes, et PostgreSQL peut ensuite garder un plan « générique » établi sans connaître le texte cherché. Le pool FastAPI fixe `plan_cache_mode = force_custom_plan` (`core/resources.py`) : plan recalculé à chaque exécution (≈ 1 ms), pour que « mot rare » et « mot courant trié par date » aient chacun le bon plan. Le test d'intégration vérifie, plans générique et personnalisé, que les requêtes générées peuvent utiliser les index trigrammes (nom, texte), `boutique_id`, `categorie_id` et l'index de date.

Toute migration Django qui modifie une colonne utilisée par les vues doit les recréer, puis relancer le script de droits ([`MODULE_CATALOGUE.md`](./MODULE_CATALOGUE.md) § 12).

## 9. Réglages (`app/core/settings.py`)

| Variable | Dev (compose) | Prod |
|---|---|---|
| `PUBLIC_BASE_URL` | `http://localhost:8001` | **obligatoire**, `https://`, pas localhost (ex. `https://anitche.com/fast`) |
| `MEDIA_BASE_URL` | `http://localhost:8000/media/` | **obligatoire**, `https://`, pas localhost |
| `SEARCH_CACHE_TTL` | 60 | 60 |

Formes normalisées (`PUBLIC_BASE_URL` sans `/` final, `MEDIA_BASE_URL` avec) ; paramètres, fragment et identifiants refusés dans ces deux URL. En prod, le service refuse de démarrer sinon.

## 10. Liste FastAPI ou liste Django ?

| Besoin du frontend | Qui sert |
|---|---|
| Barre de recherche, résultats, facettes, autocomplétion | **FastAPI** `/recherche/produits`, `/recherche/suggestions` |
| Listes publiques (catégorie, boutique, nouveautés) | **FastAPI** `/recherche/produits` sans `recherche` |
| Repli si FastAPI répond 503 (ou réseau) | **Django** `GET /api/catalogue/produits/` : même enveloppe, mêmes champs, sans facettes ni tolérance aux accents |
| Fiche produit, catégories, boutiques, panier, commandes | **Django** |

## 11. Performances mesurées

Données de démo (18 produits visibles), dans le conteneur de dev (`--reload`, un worker), un seul client, 25 requêtes par cas. **Pas une capacité de charge** (CLAUDE.md § 16).

| Cas | Cache froid (médiane / p95) | Cache chaud (médiane / p95) |
|---|---|---|
| Liste sans recherche, page 1 | 5,9 / 6,8 ms | 5,4 / 6,8 ms |
| `recherche=karite` | 11,7 / 16,5 ms | 4,8 / 5,8 ms |
| `recherche=chemize` (faute) | 7,9 / 9,6 ms | 5,5 / 6,3 ms |
| `categorie=mode&tri=prix_asc` | 7,5 / 10,5 ms | 4,7 / 5,8 ms |
| `recherche=zzzz` (aucun résultat) | 8,8 / 12,3 ms | 5,2 / 7,9 ms |
| Suggestions `chem` | 4,8 / 5,4 ms | 1,4 / 1,8 ms |

Sur des catalogues synthétiques de 10 000 et 100 000 produits (rapport de diagnostic du module 2, SQL équivalent) : recherches de 1 à 231 ms avec les index ; **tri par prix et facettes du catalogue entier ≈ 0,9 s à 100 000** (prix minimum calculé pour chaque produit). Réponse de niveau 2, **seulement sur mesure** (test de charge ou catalogue au-delà d'environ 20 000 produits) : `prix_min` et `en_stock` dénormalisés, tenus à jour par Django.

## 12. Sécurité — failles corrigées (diagnostic de septembre 2026)

| # | Avant | Après |
|---|---|---|
| 1-2 | Maquette de 6 produits en dur ; dictionnaires globaux modifiés à chaque recherche, partagés avec le conseiller IA | Vues PostgreSQL ; aucun état global ; maquette IA séparée et immuable |
| 3 | `q` sans taille maximale (60 000 caractères acceptés) | 100 caractères (50 pour les suggestions), 8 mots |
| 4, 10 | Paramètres différents de Django, `inf` accepté, page trop grande ramenée à la page 1 | Paramètres de Django, entiers bornés, 400 explicites, 404 au-delà de la dernière page |
| 5 | Facettes calculées sur la maquette entière, sans id | Facettes de l'ensemble filtré, avec id et slug |
| 6 | Tri par note sur des notes inventées | Supprimé |
| 7-9 | Sensible aux accents, aucune faute tolérée, un seul mot suffisait | Sans accents, fautes tolérées, tous les mots exigés (critères 1 et 3) |
| 11-12 | Champs inventés (`image_url` nulle, `disponible` toujours vrai, suggestions « artisanat ») | Champs réels de Django, `en_stock` réel, suggestions réelles |
| — | Jokers `LIKE` | Échappés après normalisation (pleine chasse comprise) |
| — | Lecture possible de tables sensibles | Rôle limité aux 3 vues publiques |

## 13. Tests

- `tests/test_recherche.py` : paramètres (chaque 400), SQL (valeurs toujours en paramètres, 3 vues seulement, échappement après normalisation, filtres, tris), réponse (champs exacts de Django, URL, liens), cache (facettes et suggestions en cache, résultats jamais, désactivable, panne de Redis), pannes PostgreSQL (503), aucune maquette ni état global.
- `tests/integration/test_recherche_vues.py` (vraies vues des migrations Django, rôle en lecture seule, Redis base 15) : produit masqué dans Django (produit désactivé, sans variante, variantes inactives, boutique suspendue ou fermée, vendeur KYC en attente ou refusé, vendeur inactif, propriétaire client) invisible partout ; masquage effectif à la requête suivante ; prix, stock et image comme Django ; sous-catégories ; accents et fautes (`baoule` → « Robe Baoulé ») ; paliers ; facettes cohérentes avec les résultats filtrés ; `%`, `_`, `\` et leurs formes pleine chasse ; tri et pages ; 2e appel sans SQL pour les facettes ; aucune table du catalogue lisible ; index utilisables (plans générique et personnalisé) ; `plan_cache_mode` du pool.
- `tests/integration/test_postgres_readonly.py` : droits de table du rôle = exactement les 3 vues.

Commandes locales : [`MODULE_SUIVI_GPS.md`](./MODULE_SUIVI_GPS.md) § 11 (même base de test `anitche_fastapi_test`, à migrer à nouveau après une nouvelle migration Django, puis relancer le script de droits).

## 14. Hors périmètre et suites

- **Médias en prod** : `infra/nginx/nginx.conf` sert les médias publics sous `/media/` (`catalogue/produits/`, `catalogue/categories/`, `boutiques/logos/`, `boutiques/bannieres/`) et répond 404 pour le reste ; `MEDIA_BASE_URL` vaut donc `https://<domaine>/media/`. Les URL d'images de la recherche viennent toutes de `ImageProduit.image` (`catalogue/produits/%Y/%m/`), donc d'un dossier public.
- `infra/scripts/check_prod_env.sh` ne liste pas encore `PUBLIC_BASE_URL` ni `MEDIA_BASE_URL` (le service refuse déjà de démarrer sans elles).
- Produit rangé dans une catégorie inactive : visible (règle Django actuelle, dette [`MODULE_CATALOGUE.md`](./MODULE_CATALOGUE.md) § 9) ; la vue et le test de parité suivront la décision Django.
- JIT de PostgreSQL (actif par défaut) : peut ajouter du temps de compilation sur les grosses requêtes (facettes du catalogue entier) ; à mesurer pendant la phase de performance avant de le couper.
