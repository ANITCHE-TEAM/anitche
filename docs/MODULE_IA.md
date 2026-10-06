# Module conseiller IA — contrat et règles

> Périmètre : backend FastAPI, `backend-fastapi/app/` (routeur `routeurs/conseiller_ia.py`, service `services/conseiller/`, modèles `modeles/conseiller_ia.py`, limite de taille des corps `core/body_limit.py`).
> **Aucune vraie IA n'est intégrée** (décision d'équipe) : le fournisseur actif est un fournisseur **simulé**, par règles, sans appel réseau ni clé. L'architecture accueille plus tard n'importe quel fournisseur (Gemini, OpenAI, Anthropic, Mistral, modèle local…) avec **un fichier adaptateur, une ligne de registre et une configuration**, sans toucher aux routes, aux schémas de réponse, au frontend ni aux tests existants (§ 10).

## 1. Rôle

| Route | Usage |
|---|---|
| `POST /ia/conseil` | Conversation de shopping : le client décrit un besoin (occasion, style, budget, catégories) ; le conseiller répond par un message, au plus **4 produits** du catalogue avec une justification chacun, et des conseils généraux |
| `POST /ia/recommandations` | Sélection de **8 produits** au plus selon des catégories et un budget |

Les produits viennent **toujours du vrai catalogue** (vues publiques de Django, par la recherche publique), visibles et **en stock**, au plus au prix du budget. Un fournisseur, quel qu'il soit, ne peut ni inventer un produit ni changer un prix : il choisit parmi une liste, et sa réponse est revalidée (§ 6).

## 2. Ce sur quoi le module s'appuie

| Dépendance | Usage |
|---|---|
| Recherche publique (`services/search.py` : `SearchFilters`, `fetch_page`, `product_result`) | Candidats : mêmes vues, même tolérance aux accents et aux fautes, même forme de produit. **Non modifiée** ([`MODULE_RECHERCHE.md`](./MODULE_RECHERCHE.md)) |
| Authentification du socle (`core/auth.py`) | Jeton `access` de Django, vérifié par Django (tout rôle) |
| Redis (base 2) | Limites de débit par utilisateur ; compteur du garde-fou de budget (§ 7) |
| Aucune autre base, aucun appel réseau | Le fournisseur simulé ne sort pas du processus ; FastAPI ne garde aucune conversation |

## 3. Architecture

```text
routeurs/conseiller_ia.py        conseiller_actif (503) → rate_limit(user) (401/429) → validation (400)
modeles/conseiller_ia.py         demandes bornées (champs inconnus refusés), réponses (produit de la recherche + justification)
services/conseiller/service.py   orchestration : masquage, candidats, fournisseur (délai, garde-fou, repli), validation de sortie
services/conseiller/candidats.py termes et catégories → fetch_page (5 requêtes au plus) → produits en stock, 30 au plus
services/conseiller/lexique.py   normalisation (mots entiers, sans accents), thèmes, termes cherchés
services/conseiller/fournisseurs/
    __init__.py                  registre FOURNISSEURS (AI_PROVIDER), vérifié au démarrage
    base.py                      interface FournisseurIA (classe abstraite, async), DemandeFournisseur, Candidat, SortieFournisseur
    simule.py                    fournisseur simulé (règles), code « simule »
core/body_limit.py               413 au-delà de MAX_REQUEST_BODY_BYTES (toutes routes)
```

Déroulé d'un appel à `/ia/conseil` :

1. `AI_ENABLED=false` : 503 `conseiller_desactive` (avant tout, même sans jeton) ; sinon jeton vérifié (401), limite `ai_advice` par utilisateur (429), validation (400).
2. Préparation : e-mails et numéros de téléphone du texte masqués (`[masqué]`) ; **aucun identifiant d'utilisateur** transmis au fournisseur.
3. Candidats (§ 5) : jusqu'à 30 produits visibles, en stock, au plus au prix du budget.
4. Fournisseur actif (`AI_PROVIDER`), sous délai `AI_TIMEOUT_SECONDS` ; s'il est payant, garde-fou de budget d'abord. Panne, délai dépassé, sortie invalide ou budget épuisé : **repli sur le simulé** (`source: "regles"`).
5. Validation de la sortie **hors du fournisseur** (§ 6), puis réponse : lignes du catalogue + justifications.

## 4. Contrat HTTP

Base : `http://localhost:8001` en dev, `https://anitche.com/fast` en prod ([`GUIDE_FRONTEND.md`](./GUIDE_FRONTEND.md) § 12). En-tête `Authorization: Bearer <access>` (jeton de Django) obligatoire.

### `POST /ia/conseil`

```json
{
  "messages": [
    {"role": "user", "contenu": "Je cherche une tenue pour un mariage"},
    {"role": "assistant", "contenu": "Pour une cérémonie, voici 4 articles…"},
    {"role": "user", "contenu": "Moins cher ?"}
  ],
  "occasion": "mariage",
  "style": "traditionnel",
  "budget_max": 20000,
  "categories": ["mode"]
}
```

| Champ | Règle |
|---|---|
| `messages` | Obligatoire, **1 à 10** messages. `role` : `user` (le client) ou `assistant` (réponse précédente du conseiller, renvoyée telle quelle par le frontend) ; **le dernier est `user`**. `contenu` : 1 à **1 000** caractères **après** fusion des espaces (composition Unicode NFC) ; caractères de contrôle refusés (sauf tabulation et retour à la ligne, fusionnés) |
| `occasion`, `style` | Facultatifs, texte libre de 60 caractères au plus (vide : ignoré) |
| `budget_max` | Facultatif, **entier** (FCFA) de 1 à 9 999 999 999. Refusés : 0, négatif, décimal (`30000.0` compris), texte, booléen, `NaN`, `Infinity` |
| `categories` | Facultatif, 0 à **5** slugs ou ids de catégorie (sous-catégories comprises), comme `categorie` de la recherche ; doublons fusionnés |
| Tout autre champ | **400** « Ce champ n'est pas autorisé. » (`utilisateur_id`, `fournisseur`, `prompt`…) |

Réponse 200 (données de démonstration ; un seul des 3 produits montré) :

```json
{
  "reponse": "Pour une cérémonie, voici 3 articles dans votre budget de 20 000 FCFA.",
  "produits_suggeres": [
    {
      "id": 34, "nom": "Sac en pagne tissé", "slug": "sac-en-pagne-tisse-4d5e6f",
      "prix_base": "9500.00", "prix_min": 9500.0,
      "image_principale": "http://localhost:8000/media/catalogue/produits/2026/09/produit.png",
      "miniature_principale": "http://localhost:8000/media/catalogue/miniatures/2026/09/produit.webp",
      "categorie": 5, "categorie_nom": "Mode", "boutique": 11, "boutique_nom": "Pagnes & Style",
      "boutique_slug": "pagnes-style", "en_stock": true, "date_creation": "2026-09-27T20:27:03.031043Z",
      "justification": "Pagne : une valeur sûre pour une cérémonie. 9 500 FCFA, dans votre budget."
    }
  ],
  "conseils_style": ["Pour une cérémonie, un tissu noble (bazin, kita) et une parure sobre font souvent l'unanimité."],
  "source": "regles"
}
```

- Chaque produit a **exactement les champs d'un résultat de `GET /recherche/produits`** (donc de la liste Django), plus `justification` (300 caractères au plus). Fiche : Django `GET /api/catalogue/produits/<slug>/`.
- `reponse` : 600 caractères au plus ; `conseils_style` : 3 au plus, 200 caractères chacun. **Texte brut** : jamais interprété comme du HTML, jamais de lien.
- `source` : `"regles"` (fournisseur simulé, ou repli) ou `"ia"` (fournisseur d'IA). Jamais le nom du fournisseur.
- Aucun produit : 200 avec `produits_suggeres: []` et un message qui le dit (« Aucun article en stock pour une cérémonie dans votre budget de 5 000 FCFA. Élargissez le budget ou changez de catégorie. »), jamais 404.
- Même demande, même réponse avec le fournisseur simulé (déterministe).

### `POST /ia/recommandations`

```json
{"categories": ["mode", "beaute"], "budget_max": 20000}
```

Les deux champs sont facultatifs (`{}` : tout le catalogue) ; mêmes règles que ci-dessus ; tout autre champ, dont `utilisateur_id` et `categories_preferees`, est refusé (400). Réponse : `{"recommandations": [ …8 produits au plus, même forme… ], "source": "regles"}`.

### Erreurs

Format commun ([`GUIDE_FRONTEND.md`](./GUIDE_FRONTEND.md) § 5) :

| HTTP | `errors.code[0]` | Quand |
|---|---|---|
| 400 | — (clé du champ : `messages.0.contenu`, `budget_max`, `utilisateur_id`…) | Validation |
| 401 | — | Jeton absent, invalide ou expiré (`WWW-Authenticate: Bearer`) |
| 413 | `corps_trop_volumineux` | Corps au-delà de `MAX_REQUEST_BODY_BYTES` (128 Kio par défaut), refusé avant d'être lu |
| 429 | — | Limite par utilisateur dépassée (`Retry-After`) |
| 503 | `conseiller_desactive` | `AI_ENABLED=false` : conseiller coupé |
| 503 | `conseiller_indisponible` | Catalogue (PostgreSQL) injoignable : aucun conseil possible |
| 503 | — | Redis ou vérification du jeton indisponible (socle) |

Une panne du **fournisseur** n'est jamais une erreur : c'est un repli sur le simulé (`source: "regles"`).

## 5. Candidats et lexique

Candidats (`candidats.py`), requêtes **séquentielles**, 20 lignes chacune, **5 au plus** :

- un **terme** par requête (4 au plus), dans la catégorie demandée s'il n'y en a qu'une ; avec plusieurs catégories, les termes servent seulement au classement (la recherche filtre une catégorie à la fois) ;
- si rien n'est trouvé : une requête par catégorie demandée, sinon le catalogue entier (les plus récents) ;
- prix affiché au plus égal au budget (SQL), **produits en rupture exclus**, dédoublonnés, **30 au plus**.

Lexique (`lexique.py`, des **données** à ajuster sur le vrai catalogue) : comparaison sur des **mots entiers**, sans accents ni majuscules, au singulier approché. « besoin » ne déclenche pas « soin » ; « cérémonie » et « ceremonie » sont le même mot. Seuls les 3 derniers messages du client, `occasion` et `style` sont lus (les messages `assistant` sont forgeables).

| Thème | Déclencheurs | Termes cherchés | Catégorie favorisée |
|---|---|---|---|
| cérémonie | mariage, dot, baptême, cérémonie, fête, gala, soirée, fiançailles, chic | bazin, pagne, kita, boubou | Mode |
| bureau | bureau, travail, réunion, entretien | chemise, wax, pantalon | Mode |
| maison ou cadeau | cadeau, maison, déco, décoration, cuisine, souvenir | nappe, mortier, marmite | Maison |
| soins | beauté, soin, peau, cheveux, karité, savon, cosmétique | karité, savon, huile | Beauté |
| électronique | téléphone, smartphone, portable, écouteur, batterie, chargeur | smartphone, écouteurs, batterie | Électronique |

Termes : d'abord les mots de produit cités par le client (« marmite »), puis ceux des thèmes reconnus (2 au plus), 4 au total.

## 6. Validation de la sortie (tous fournisseurs)

Dans `service.py`, jamais dans le fournisseur :

| Étape | Règle |
|---|---|
| Schéma | `SortieFournisseur` (Pydantic) à partir d'un dictionnaire ou d'un texte JSON : `produits` (obligatoire, 50 au plus, `id` entier strictement positif, `justification` ≤ 500), `message` (≤ 2 000), `conseils` (5 au plus, ≤ 300). Hors schéma : **repli** sur le simulé |
| Identifiants | Identifiant absent des candidats **rejeté**, doublons retirés, coupé à 4 (conseil) ou 8 (recommandations) |
| Textes | Liens (`http…`, `www.…`) retirés ; caractères de contrôle et de format (dont les inversions de sens d'écriture) remplacés ; espaces fusionnés ; longueurs bornées (300, 600, 3 × 200) ; justification vide → « Correspond à votre demande. » |
| Message | Remplacé par un gabarit du service si un identifiant a été rejeté (le message pouvait citer le produit inventé) ou s'il contenait un lien |
| Réponse | Lignes du catalogue reprises telles quelles : nom, prix, image viennent de la base, jamais du fournisseur |

## 7. Sécurité

| Mesure | Détail |
|---|---|
| Authentification | Obligatoire, tout rôle (les réponses ne contiennent que le catalogue public) |
| Limites | `ai_advice` **20/h** par utilisateur (`/ia/conseil`), `ai_recommendations` **120/h** par utilisateur (`/ia/recommandations`), réglables par `RATE_LIMITS` |
| Taille | Champs bornés (§ 4) ; corps de requête au plus `MAX_REQUEST_BODY_BYTES` (128 Kio) pour **toutes** les routes FastAPI, refusé en 413 **avant** lecture (Content-Length) ou dès le dépassement (corps sans Content-Length) |
| Données personnelles | Aucun identifiant d'utilisateur ni e-mail de compte transmis ; e-mails et numéros (8 chiffres ou plus) masqués dans les textes du client. Limite connue : un nom propre saisi reste |
| Injection de consignes | Défense **structurelle**, pas de détection par mots-clés (contournable, faux positifs) : historique traité comme une donnée, rôle `system` refusé, consignes fixes dans l'adaptateur, sortie limitée aux identifiants candidats, liens retirés, aucun outil ni secret donné au modèle. Points de renforcement : `service.conseiller()` (préparation) et `service.valider_sortie()` |
| Délai | `AI_TIMEOUT_SECONDS` (10 s) par appel au fournisseur ; SQL déjà borné (`statement_timeout` 3 s) |
| Garde-fou de budget | Fournisseur **payant** seulement : compteur Redis `fastapi:ia:appels:<AAAA-MM-JJ>` (UTC, expiration 2 jours) ; au-delà de `AI_DAILY_CALL_LIMIT` (1 000/jour), ou si Redis ne répond pas, repli **sans** appel. Jamais d'appel payant sans compteur |
| Journaux | Jamais le contenu des messages ni la réponse ; seulement le fournisseur, la cause d'un repli et sa durée |
| Conversations | **Aucune persistance** : l'historique vient du frontend à chaque appel |

## 8. Réglages

| Variable | Défaut | Rôle |
|---|---|---|
| `AI_ENABLED` | `true` | `false` : 503 `conseiller_desactive` sur les deux routes |
| `AI_PROVIDER` | `simule` | Clé du registre `FOURNISSEURS` ; valeur inconnue : **refus de démarrer** (« AI_PROVIDER inconnu : « gemini » (valeurs possibles : simule) »). `simule` autorisé en dev **et** en prod (décision d'équipe) |
| `AI_TIMEOUT_SECONDS` | `10` | Délai maximal d'un appel au fournisseur (0 < x ≤ 30) ; au-delà, repli |
| `AI_DAILY_CALL_LIMIT` | `1000` | Appels payants par jour, tous utilisateurs (0 : aucun appel payant) ; sans effet sur le simulé |
| `MAX_REQUEST_BODY_BYTES` | `131072` | Taille maximale d'un corps de requête, toutes routes (1 Kio à 1 Mio) |
| `RATE_LIMITS` | — | Peut remplacer `ai_advice` (20/hour) et `ai_recommendations` (120/hour) |

Aucune variable n'est à ajouter aux composes tant que le fournisseur est `simule`.

## 9. Fournisseur simulé

Déterministe, sans IA ni réseau (`payant = False`, `source = "regles"`). Classement des candidats :

- pertinence : 3 par terme cherché présent dans le **nom** du produit, 1 par terme dans le nom de la **boutique**, 2 si la **catégorie** est celle d'un thème reconnu ;
- à pertinence égale : produit qui utilise au moins la moitié du budget, puis ordre de la recherche, puis id.

S'il existe des produits pertinents, seuls ceux-là sont proposés ; sinon, les premiers du catalogue, en le disant (« Rien de précis pour l'électronique ; voici 3 articles du catalogue. »). Textes en gabarits factuels (terme, catégorie, boutique, prix, budget) : **jamais** de certification, d'origine, de promesse ni de lien. Exemple sur les données de démonstration, « une tenue pour un mariage », budget 30 000 : Robe en bazin brodé, Sac en pagne tissé, Chemise en wax, Nappe en kita.

## 10. Ajouter un fournisseur

Aucun SDK : `httpx` suffit (déjà une dépendance). Étapes :

1. Écrire `app/services/conseiller/fournisseurs/<code>.py` : une classe `FournisseurIA` (`code`, `source = "ia"`, `payant`), `async conseiller(demande) -> dict | str` (forme de `SortieFournisseur`), `fermer()`, et `creer(settings)`. Ses propres variables (`AI_<CODE>_API_KEY`…) dans une petite classe `BaseSettings` du même fichier ; **clé obligatoire en prod** vérifiée dans le constructeur (refus de démarrer).
2. Ajouter une ligne dans `FOURNISSEURS` (`fournisseurs/__init__.py`).
3. Écrire son test (`httpx.MockTransport` : aucun appel réseau en test).
4. En prod : `AI_PROVIDER=<code>` et la clé dans `infra/docker-compose.prod.yml`.

Règles communes (docstring de `base.py`) : consignes **fixes** dans l'adaptateur ; historique du client (y compris `assistant`) placé comme **donnée**, jamais dans les consignes ; ne choisir que parmi `demande.candidats` ; lever `ErreurFournisseurIA` en cas d'échec ; ne jamais journaliser l'historique, la réponse ni la clé. Le service fait le reste : délai, garde-fou, validation, repli. Routes, schémas, frontend et tests existants **ne changent pas**. Un squelette commenté figure dans le rapport d'étape (`_archives/rapport_fastapi_module4.md`, § 2 b).

## 11. Points du contrat à connaître côté frontend

| Point | Comportement |
|---|---|
| Accès | Routes authentifiées, limites par utilisateur (20/h et 120/h) |
| Produits proposés | Produits visibles et en stock du vrai catalogue |
| Forme d'un produit | Champs d'un résultat de recherche (`slug`, `prix_min`, `image_principale`, `miniature_principale`, `boutique` = **id**…) + `justification`. `/ia/conseil` (`produits_suggeres`) et `/ia/recommandations` (`recommandations`) renvoient donc aussi `miniature_principale` (miniature WebP de la même image que `image_principale`, `null` si absente : se replier sur `image_principale`) ; champ ajouté, rien d'existant ne change |
| `budget_max` | Entier de 1 à 9 999 999 999 |
| `messages` | Rôles `user` et `assistant`, dernier message `user` ; tailles bornées (`system` refusé) |
| Champs inconnus (dont `utilisateur_id`) | Refusés (400) |
| Catégories | `categories` (slugs ou ids, comme la recherche) ; `categories_preferees` refusé |
| `source` | Présent dans les deux réponses |
| Taille du corps | 413 au-delà de 128 Kio, toutes routes FastAPI |

## 12. Tests

- Unitaires : `tests/test_conseiller_ia.py` (routes, validation, ancrage, contrat, OpenAPI), `tests/test_conseiller_fournisseurs.py` (registre, réglages, validation de sortie avec de faux fournisseurs, délai, pannes, garde-fou, lexique, simulé), `tests/test_core_body_limit.py`.
- Intégration (`tests/integration/test_conseiller_vues.py`, vrais PostgreSQL et Redis) : produit masqué dans Django, en rupture ou hors budget jamais proposé ; prix affiché réel ; sous-catégories ; masquage effectif à la requête suivante ; compteur du garde-fou.

## 13. Collection Postman

`postman/collections/postman_conseiller_ia.json` (hors dépôt, à côté des autres collections) : mise en place (connexion des comptes de démo), parcours nominal, contrôles `[SEC]` (sans jeton, champ inconnu, message trop long, identifiants inventés impossibles, limite de débit).

## 14. Limites connues

- Le lexique est court et écrit à la main : il couvre les données de démonstration, pas encore tout le catalogue réel.
- La vue publique n'expose pas la description brute des produits (seulement `texte_normalise`) : un vrai fournisseur n'a que nom, catégorie, boutique et prix. Pour lui donner une description courte, il faudra une migration Django de la vue (avec son test de parité).
- Aucune personnalisation par l'historique d'achats (données de Django, hors de portée du rôle en lecture seule).
