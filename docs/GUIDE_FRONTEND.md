# Guide frontend — brancher l'API Django d'ANITCHE

> Document vivant. Pour qui développe le frontend (React) contre le backend Django.
> Les règles métier détaillées de chaque module sont dans les `MODULE_*.md` (index au § 10).

## 1. Démarrer en local

```bash
# depuis la racine du dépôt
docker compose -f infra/docker-compose.yml up -d

# une fois la stack démarrée : données de démonstration (§ 2)
docker exec anitche-backend python manage.py seed_demo
```

| Service | Adresse |
|---|---|
| Frontend (Vite) | http://localhost:5173 |
| API Django | http://localhost:8000/api/ |
| Swagger UI | http://localhost:8000/api/docs/ |
| ReDoc | http://localhost:8000/api/redoc/ |
| Schéma OpenAPI brut | http://localhost:8000/api/schema/ |
| Mailpit (emails de dev) | http://localhost:8025 |

Le conteneur `anitche-backend` applique les migrations au démarrage. Sans Docker, voir [`GUIDE_STRUCTURE_ANITCHE.md`](./GUIDE_STRUCTURE_ANITCHE.md) § 4.

## 2. Données et comptes de démonstration

> ⚠️ **DEV UNIQUEMENT.** Le mot de passe ci-dessous est public : ces comptes n'existent que sur les bases de développement. La commande refuse de tourner en production (DEBUG désactivé, settings de production ou fournisseur de paiement réel), et l'app `apps.demo` n'y est même pas installée.

```bash
python manage.py seed_demo           # crée les données (sans effet si elles existent déjà)
python manage.py seed_demo --reset   # les supprime puis les recrée
```

Tous les comptes ont le mot de passe **`Demo-Anitche-2026!`** et un email déjà vérifié.

| Rôle | Email | Ce qu'on y trouve |
|---|---|---|
| Administrateur | `admin@demo.anitche.test` | validation des vendeurs, assignation des livreurs, modération |
| Support | `support@demo.anitche.test` | un ticket ouvert par la cliente, avec une réponse |
| Client | `client@demo.anitche.test` | 5 commandes, un retour, des points de fidélité, un coupon, un ticket |
| Livreur | `livreur@demo.anitche.test` | 2 livraisons effectuées (code de livraison saisi) |
| Vendeur — Pagnes & Style | `vendeur.mode@demo.anitche.test` | commande livrée avec une demande de retour approuvée |
| Vendeur — Adjamé Tech | `vendeur.tech@demo.anitche.test` | livraison offerte ; un produit sous le seuil de stock |
| Vendeur — Maison Akwaba | `vendeur.maison@demo.anitche.test` | une commande en préparation, une commande annulée |
| Vendeur — Karité Doré | `vendeur.beaute@demo.anitche.test` | une commande payée à préparer |

Contenu créé :

- **Catalogue** : 4 catégories (Mode, Électronique, Maison, Beauté), 4 boutiques publiques de vendeurs validés, 12 produits avec variantes, prix promotionnels, stock et une image générée par produit (logo de boutique compris).
- **Commandes de la cliente** : livrée, livrée avec un retour approuvé, en préparation, payée (`confirmee`), annulée par la cliente. Les paiements sont passés par le fournisseur simulé (notification signée), comme en réel. Seule la commande **en préparation** (Maison Akwaba) a un **point GPS** (environ Angré, Cocody : `5.397340`, `-3.986620`) : c'est la prochaine à livrer, pour tester le suivi du livreur et l'estimation d'arrivée. Les autres n'en ont pas (cas « sans point »).
- **Fidélité** : 65 points crédités sur la commande livrée (le délai de rétractation est simulé par une horloge décalée), dont 50 convertis en coupon de 5 % ; les points de la commande avec retour restent en attente.
- **Support** : un ticket sur la commande en préparation.

Tout passe par l'API réelle (mêmes vues, permissions et services que le frontend). `--reset` ne supprime que les comptes `@demo.anitche.test` et ce qui leur est rattaché ; les catégories sont conservées. Si une donnée hors démo en dépend (par exemple une commande passée par un vrai compte dans une boutique de démo), la commande refuse et ne supprime rien.

## 3. Documentation interactive (Swagger)

- **Swagger UI** (http://localhost:8000/api/docs/) : bouton **Authorize**, coller le jeton `access` (sans le préfixe `Bearer`). L'autorisation est conservée au rechargement de la page.
- Endpoints regroupés par module (tags), réponses d'erreur documentées au format commun (composant `Erreur`).
- Les notifications des fournisseurs de paiement (webhooks) n'y figurent pas : le frontend ne les appelle jamais.
- **Absente en production** (décision d'équipe, vérifiée par un test) : ne jamais faire dépendre le frontend de `/api/schema/` à l'exécution. La référence est le fichier versionné `backend-django/schema.yaml`, que la CI vérifie à chaque push (schéma sans avertissement et à jour).

## 4. Authentification (JWT)

1. `POST /api/utilisateurs/connexion/` avec `{"email", "password"}` → `{"access", "refresh"}`.
2. Chaque requête authentifiée : en-tête `Authorization: Bearer <access>`.
3. `access` expire au bout de **15 minutes** → réponse **401**. Appeler alors `POST /api/utilisateurs/connexion/rafraichir/` avec `{"refresh"}` : il renvoie un nouveau couple. Le `refresh` (7 jours) est **à usage unique** : toujours remplacer celui qu'on a stocké par le nouveau. Rejouer un ancien `refresh` échoue en 401 : renvoyer vers la connexion.
4. Déconnexion : `POST /api/utilisateurs/deconnexion/` avec `{"refresh"}` (révoque le jeton).
5. Un changement de mot de passe invalide tous les jetons existants.
6. **Email non vérifié** : certaines actions (commander, devenir vendeur, changer de contact) répondent **403** avec `errors.code = ["email_non_verifie"]`. Afficher alors l'écran de saisie du code (voir [`MODULE_UTILISATEURS.md`](./MODULE_UTILISATEURS.md) § 11).

Un intercepteur axios qui rafraîchit sur 401 puis rejoue la requête une seule fois suffit ; sérialiser les rafraîchissements concurrents (un seul appel à la fois), sinon deux onglets consomment le même `refresh`.

## 5. Format d'erreur

Toutes les erreurs de l'API ont la même forme :

```json
{
  "success": false,
  "status_code": 400,
  "detail": "adresse_livraison.commune: Ce champ est obligatoire.",
  "errors": {
    "adresse_livraison.commune": ["Ce champ est obligatoire."]
  }
}
```

- `detail` : toujours une chaîne, le message principal à afficher.
- `errors` : toujours un objet (jamais `null`), clé → **liste** de messages. `{}` quand aucun champ n'est en cause (401, 403, 404, refus métier…).
- Champs imbriqués : clé à points (`adresse_livraison.commune`, `articles.0.quantite`) ; erreur sans champ : `non_field_errors`.
- Codes à traiter : **400** validation ou refus métier, **401** non authentifié ou jeton expiré, **403** interdit, **404** introuvable (aussi pour une ressource d'un autre utilisateur ; `detail` vaut alors « Ressource introuvable. », sauf message plus précis de la route), **409** transition impossible (annuler une commande en préparation…), **429** limite de débit (§ 7), **503** service indisponible, **500** erreur interne (message générique, rien de technique).
- Seule exception : les webhooks des fournisseurs de paiement, qui ne concernent pas le frontend.

## 6. Pagination

Les listes sont paginées par défaut :

```json
{ "count": 42, "next": "http://…?page=3", "previous": "http://…?page=1", "results": [ … ] }
```

20 éléments par page, paramètre `?page=N`. Exception : `GET /api/catalogue/categories/` renvoie un tableau simple (liste courte, ordonnée pour l'affichage). Swagger indique pour chaque liste si elle est paginée.

## 7. Limites de débit (429)

Au-delà d'une limite, l'API répond **429** au format commun ; l'en-tête **`Retry-After`** donne le nombre de secondes à attendre. Afficher un message (« Trop de tentatives, réessayez dans N minutes ») et désactiver le bouton, sans relancer automatiquement.

Limites les plus visibles côté interface (production) : connexion 10/h, envoi de code OTP 5/h, inscription 10/h par IP, validation du panier 20/h, paiements 20/h, conversion de points 20/h, vérification de coupon 30/h. La liste complète est dans `REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']` (`config/settings/base.py`). En dev, certaines sont relevées pour Postman (`config/settings/dev.py`) : ne pas en déduire le comportement de production.

## 8. Conventions de données

- **Montants en FCFA entiers, reçus en chaînes décimales.** Tous les champs monétaires (`prix`, `montant_total`, `frais_livraison`…) arrivent comme des chaînes : `"65000.00"`, partie décimale toujours nulle. Ne jamais les additionner tels quels (`"1500.00" + "500.00"` concatène) :

  ```js
  // Chaîne décimale de l'API → entier FCFA (null reste null, ex. prix_promo absent)
  const versFcfa = (valeur) => (valeur == null ? null : Math.round(Number(valeur)));
  // Affichage : « 65 000 FCFA »
  const afficherFcfa = (valeur) => `${new Intl.NumberFormat('fr-FR').format(versFcfa(valeur))} FCFA`;
  ```

  En envoi (prix d'un produit, montant d'un coupon à vérifier), transmettre un entier (`15000`) ou sa chaîne (`"15000"`) ; les prix du catalogue (`prix`, `prix_promo`, `prix_base`) avec centimes sont refusés (400). Les montants sont positifs, sauf `montant_ajustements` (toujours ≤ 0) et `montant_net` d'un reversement. `poids_kg`, `taux_commission` (pourcentage) et `valeur` d'un coupon (pourcentage ou montant) peuvent avoir des décimales. Swagger montre pour chaque champ un exemple réaliste (`"15000.00"`).
- **Identifiants** : entiers pour les utilisateurs, boutiques et produits ; UUID pour les commandes, paiements, livraisons, retours et tickets.
- **Envois de fichiers** (`multipart/form-data`) : KYC, logo de boutique, images produit, photos de retour, pièces jointes du support. Un booléen **absent** du formulaire est ignoré, comme en JSON : il prend la valeur par défaut à la création (une boutique créée avec son logo est ouverte, un produit est actif) et garde sa valeur actuelle en modification. Pour changer un booléen, l'envoyer explicitement (`est_active=false`).
- **Fichiers protégés** (KYC, photos de retour, pièces jointes) : jamais d'URL média directe ; les télécharger via leurs endpoints dédiés, avec le jeton.

## 9. Emails de dev et types TypeScript

**Mailpit** (http://localhost:8025) reçoit tous les emails envoyés en dev : codes OTP, code de livraison, notifications. Aucun email ne sort vers l'extérieur. Détails : [`GUIDE_STRUCTURE_ANITCHE.md`](./GUIDE_STRUCTURE_ANITCHE.md) § 4.

**Types générés depuis le schéma** avec [openapi-typescript](https://openapi-ts.dev) (OpenAPI 3.1) :

```bash
cd frontend
npx openapi-typescript ../backend-django/schema.yaml -o src/services/api-types.d.ts
```

À relancer à chaque modification de `schema.yaml`. Le frontend étant en JavaScript, les types s'utilisent via JSDoc (l'éditeur les vérifie) :

```js
/** @typedef {import('./api-types').components['schemas']['ProduitPublicDetail']} ProduitDetail */
```

Les corps de requête ont leur propre composant (`…Request`, sans les champs en lecture seule) ; les énumérations ont des noms stables (`StatutCommandeEnum`, `StatutLivraisonEnum`…).

## 10. Index des « Impact frontend »

Chaque module documente les changements de contrat à intégrer côté interface :

| Module | Section |
|---|---|
| Utilisateurs | [`MODULE_UTILISATEURS.md` § 11](./MODULE_UTILISATEURS.md#11-impact-frontend) |
| Commandes | [`MODULE_COMMANDES.md` § 9](./MODULE_COMMANDES.md#9-impact-frontend) |
| Paiements | [`MODULE_PAIEMENTS.md` § 10](./MODULE_PAIEMENTS.md#10-impact-frontend) |
| Livraison | [`MODULE_LIVRAISON.md` § 10](./MODULE_LIVRAISON.md#10-impact-frontend) |
| Suivi GPS (FastAPI) | § 12 ci-dessous et [`MODULE_SUIVI_GPS.md`](./MODULE_SUIVI_GPS.md) |
| Recherche (FastAPI) | § 12 ci-dessous et [`MODULE_RECHERCHE.md`](./MODULE_RECHERCHE.md) |
| Scan QR (FastAPI) | § 12 ci-dessous et [`MODULE_SCAN_QR.md`](./MODULE_SCAN_QR.md) |
| Retours | [`MODULE_RETOURS.md` § 8](./MODULE_RETOURS.md#8-impact-frontend) |
| Fidélité | [`MODULE_FIDELITE.md` § 6](./MODULE_FIDELITE.md#6-impact-frontend) |
| Notifications | [`MODULE_NOTIFICATIONS.md` § 5](./MODULE_NOTIFICATIONS.md#5-impact-frontend) |
| Support | [`MODULE_SUPPORT.md` § 6](./MODULE_SUPPORT.md#6-impact-frontend) |
| Vendeurs, catalogue, panier, passeports QR | pas de section dédiée : le contrat de chaque endpoint est dans le `MODULE_*.md` correspondant |

## 11. Point GPS au checkout (« ma position »)

Facultatif. Le checkout (`POST /api/commandes/valider-panier/` et `simuler-frais/`) fonctionne sans point, comme avant. Avec un point, le livreur peut naviguer jusqu'au lieu exact, et le suivi peut estimer son arrivée. **Sans point, aucune distance ni estimation n'est affichée** : ne pas en inventer une.

```json
{
  "adresse_livraison": {
    "commune": "Cocody",
    "quartier": "Angré 8e Tranche",
    "point_de_repere": "Derrière la pharmacie",
    "telephone": "0707070707",
    "latitude": 5.359952,
    "longitude": -3.986912
  }
}
```

Règles du serveur ([`MODULE_COMMANDES.md`](./MODULE_COMMANDES.md) § 3 bis) :

- `latitude` et `longitude` sont des **nombres** (jamais des chaînes, pas de `toFixed()`), envoyés **ensemble** ou pas du tout (`null` vaut absent).
- Côte d'Ivoire uniquement : latitude de 4 à 11, longitude de -9 à -2. Le serveur arrondit à 6 décimales.
- Refus **400** sur `errors["adresse_livraison.latitude"]` ou `errors["adresse_livraison.longitude"]` : coordonnée seule, point hors de Côte d'Ivoire (géolocalisation d'un ordinateur via un VPN, par exemple), valeur non numérique. Afficher « Position non reconnue » et proposer de **continuer sans point**.
- En lecture, `adresse_livraison.latitude` / `longitude` (détail de commande, groupes) et `adresse.latitude` / `longitude` (suivi de livraison) sont des nombres, ou `null` sans point.

Bouton « Utiliser ma position » :

```js
// Position de l'appareil, ou null (refus, délai dépassé, navigateur sans
// géolocalisation) : le checkout continue alors sans point.
function lirePosition() {
  return new Promise((resoudre) => {
    if (!('geolocation' in navigator)) return resoudre(null);
    navigator.geolocation.getCurrentPosition(
      ({ coords }) => resoudre({ latitude: coords.latitude, longitude: coords.longitude, precision: coords.accuracy }),
      () => resoudre(null),
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 },
    );
  });
}

// Au clic sur le bouton (jamais au chargement de la page) :
const position = await lirePosition();

const corps = {
  adresse_livraison: {
    commune, quartier, point_de_repere, telephone,
    // Des nombres, les deux ensemble ; rien du tout sans position.
    ...(position && { latitude: position.latitude, longitude: position.longitude }),
  },
};
```

- Demander la position **seulement au clic**, avec une phrase claire : « Utiliser ma position actuelle comme lieu de livraison ». Le client doit être **sur le lieu de livraison** : sinon il laisse le champ vide et se fie au point de repère. Afficher `precision` (mètres) aide à juger un point approximatif.
- La géolocalisation du navigateur exige **HTTPS** (ou `localhost` en dev).
- Pour changer de lieu, redemander la position ou retirer le point (bouton « Ne pas utiliser ma position »).

> **Vie privée.** Ce point est l'emplacement précis du domicile du client. L'API ne le montre qu'**au client**, **au livreur assigné pendant la livraison** (masqué une fois la livraison livrée ou annulée) et **à l'administration** ; **jamais au vendeur**. Côté interface : ne pas l'afficher aux autres rôles, ne pas le garder dans `localStorage` au-delà du checkout, ne pas l'envoyer à un service tiers (analytics, logs).

## 12. Service FastAPI (`/fast/`)

Second backend, pour le temps réel et les services rapides : **suivi GPS du livreur**, contrat complet dans [`MODULE_SUIVI_GPS.md`](./MODULE_SUIVI_GPS.md), **recherche du catalogue** (ci-dessous et [`MODULE_RECHERCHE.md`](./MODULE_RECHERCHE.md)), **scan QR des passeports** (ci-dessous et [`MODULE_SCAN_QR.md`](./MODULE_SCAN_QR.md)) et **conseiller IA** (ci-dessous et [`MODULE_IA.md`](./MODULE_IA.md)).

### Base et authentification

| | Dev | Prod |
|---|---|---|
| HTTP | `http://localhost:8001` (`VITE_FASTAPI_URL`) | `https://anitche.com/fast` |
| WebSocket | `ws://localhost:8001` | `wss://anitche.com/fast` |

- **HTTP** : le **même jeton `access` que Django** (§ 4), en-tête `Authorization: Bearer <access>`. Sur **401**, rafraîchir le jeton auprès de Django (§ 4) puis rejouer : l'intercepteur axios de Django convient.
- **Format d'erreur** : identique à Django (§ 5). Les refus du suivi portent un **code machine** : se fier à `errors.code[0]`, **jamais au texte** de `detail`.
- **429** : en-tête `Retry-After`, exposé par CORS (§ 7).
- **413** `errors.code[0] = "corps_trop_volumineux"` : corps de requête au-delà de 128 Kio, sur toutes les routes FastAPI (aucune requête légitime n'en approche).

### Suivi GPS : qui fait quoi

| Rôle | Action |
|---|---|
| Livreur (app) | Pendant `en_cours` (statut lu dans Django) : `POST /livraison/position` toutes les **5 s**. **Arrêter** sur 403, 404 ou 409 |
| Client, administration | Écran de suivi pendant `en_cours` : WebSocket (ci-dessous), repli `GET /livraison/position/{livraison_id}` toutes les 5 s |
| Vendeur | Aucun suivi de ses ventes (il suit ses propres achats comme un client) |

```json
{
  "livraison_id": "d27b428a-ed14-4284-bc44-de11f3552bd5",
  "latitude": 5.32, "longitude": -4.015,
  "vitesse_kmh": 28.0, "cap_degres": 40.0,
  "horodatage": "2026-09-27T23:28:12.251Z",
  "distance_restante_km": 12.8, "temps_estime_minutes": 39
}
```

- **Distance et temps restants : indicatifs** (vol d'oiseau corrigé d'un facteur de détour, vitesse moyenne urbaine, **sans trafic**). Afficher « environ 39 min », jamais une heure d'arrivée précise.
- **`null` sans point GPS du client** (§ 11, point facultatif au checkout) : **ne rien afficher**, ne pas inventer de valeur.
- `horodatage` : UTC (le `Z`) ; garder la position la plus récente.

Codes machine (`errors.code[0]`) :

| Code HTTP | Code | Réaction |
|---|---|---|
| 404 | `aucune_position` | Pas encore de position (ou plus) : afficher « Position du livreur bientôt disponible », **pas une erreur** |
| 404 | `livraison_introuvable` | Livraison inexistante ou d'un autre compte |
| 403 | `acces_reserve_livreurs` | App livreur : compte qui n'est plus livreur, arrêter d'émettre |
| 403 | `livraison_non_assignee` | App livreur : livraison réassignée, arrêter d'émettre |
| 409 | `livraison_pas_en_cours` | App livreur : la tournée est finie (ou pas commencée), arrêter d'émettre |
| 400 | clés de champ | `errors.livraison_id`, `errors.latitude`… : bug du client |

### WebSocket `/livraison/ws/{livraison_id}`

Un navigateur ne peut pas poser d'en-tête `Authorization` sur un WebSocket, et une URL finit dans les journaux : **le jeton n'est jamais dans l'URL**, il part dans le **premier message**.

```js
function suivreLivraison(livraisonId, { jeton, rafraichirJeton, surPosition, surFin }) {
  let socket, renouvellement, essais = 0, fermeVolontairement = false, derniere = null;

  function ouvrir() {
    socket = new WebSocket(`${WS_FASTAPI}/livraison/ws/${livraisonId}`);
    socket.onopen = () => socket.send(JSON.stringify({ type: 'auth', token: jeton() }));
    socket.onmessage = ({ data }) => {
      const message = JSON.parse(data);
      if (message.type === 'authentifie') {
        essais = 0;
        // Renouveler le jeton avant ses 15 minutes, sans se reconnecter.
        renouvellement = setInterval(async () => {
          socket.send(JSON.stringify({ type: 'auth', token: await rafraichirJeton() }));
        }, 10 * 60 * 1000);
      } else if (message.type === 'position') {
        // Doublon possible à la connexion : garder la plus récente.
        if (!derniere || message.horodatage >= derniere.horodatage) { derniere = message; surPosition(message); }
      } else if (message.type === 'fin_suivi') {
        surFin(message.statut);
      }
    };
    socket.onclose = async ({ code }) => {
      clearInterval(renouvellement);
      if (fermeVolontairement || [1000, 1008, 1009, 4403].includes(code)) return; // relire la livraison dans Django
      if (code === 4401) await rafraichirJeton();
      const attente = code === 4429 ? 60000 : Math.min(30000, 1000 * 2 ** essais++);
      setTimeout(ouvrir, attente); // 1011, 1006 (réseau), 4401, 4429
    };
  }

  ouvrir();
  return () => { fermeVolontairement = true; socket.close(); };
}
```

- Ouvrir le WebSocket **seulement** si la livraison est `en_cours` (Django). Envoyer `auth` **dans les 5 s**.
- Seul message accepté du client : `{"type": "auth", "token"}` (connexion, puis renouvellement), **6 par minute** au plus, 4 096 octets au plus. Tout autre message ferme la connexion (1008).
- Messages reçus : `authentifie`, `position` (format du `GET`, avec `"type": "position"`), `fin_suivi` (`statut` : `livree`, `echouee`…) juste avant la fermeture 1000.

Codes de fermeture (se fier au **code**, pas au motif) :

| Code | Signification | Reconnexion |
|---|---|---|
| 1000 | Suivi terminé (après `fin_suivi`) | Non : relire la livraison dans Django |
| 4403 | Hors périmètre, pas `en_cours`, ou droits perdus | Non : relire la livraison dans Django |
| 4401 | Pas d'`auth` en 5 s, jeton refusé ou expiré | Oui, après avoir rafraîchi le jeton |
| 4429 | Trop de connexions (60/h) ou de messages | Oui, après une longue attente |
| 1011 | Service indisponible | Oui, délai croissant |
| 1006 | Coupure réseau | Oui, délai croissant |
| 1008, 1009 | Message non prévu ou trop grand | Non : bug du client |

Le serveur revalide le jeton et les droits toutes les 60 s : un compte désactivé ou une livraison réassignée ferme la connexion en 4403, une livraison terminée en 1000.

### Recherche et listes de produits

Contrat complet : [`MODULE_RECHERCHE.md`](./MODULE_RECHERCHE.md). Routes **publiques** (aucun jeton).

| Écran | Appel |
|---|---|
| Barre de recherche, page de résultats, filtres, facettes | FastAPI `GET /recherche/produits` |
| Listes publiques (catégorie, boutique, nouveautés) | FastAPI `GET /recherche/produits` sans `recherche` |
| Autocomplétion | FastAPI `GET /recherche/suggestions` |
| **Repli** si FastAPI répond 503 ou ne répond pas | Django `GET /api/catalogue/produits/` (mêmes paramètres, sauf `tri=pertinence`) |
| Fiche produit, catégories, boutiques | **Django** (`/api/catalogue/produits/<slug>/`…) |

- **Paramètres** : ceux de la liste Django (`recherche`, `categorie`, `boutique`, `prix_min`, `prix_max`, `tri`, `page`), plus `tri=pertinence` (défaut quand `recherche` est rempli). Recherche **sans accents ni majuscules**, **fautes de frappe tolérées**, aussi dans les noms de boutique et de catégorie. Valeur invalide : **400** (`errors.<paramètre>`), là où Django l'ignore ; ne pas envoyer de paramètre vide autre que `recherche`.
- **Réponse** : `{count, next, previous, results}` avec **les mêmes éléments que Django** : un seul composant « carte produit » sert aux deux. Différence : produit sans catégorie → `categorie_nom: null` (Django omet la clé). Plus `facettes` (`categories`, `boutiques`, `prix.tranches`), en **page 1 seulement** (`null` ensuite).
- `next` / `previous` : URL absolues, à suivre telles quelles. Page au-delà de la dernière : 404 `errors.code[0] = "page_invalide"`. 50 pages au plus.
- **Stock** : seulement `en_stock` (« En stock » / « Rupture ») ; jamais de quantité.
- `count` et facettes peuvent avoir **jusqu'à 60 s de retard** (cache) ; la liste elle-même est toujours à jour.
- **Autocomplétion** : rien sous **3 caractères**, anti-rebond de **300 ms**, annuler la requête précédente ; 8 suggestions par défaut (`limite` 1 à 10), chacune `{type, texte, id, slug}` avec `type` = `categorie`, `boutique` ou `produit`, pour naviguer directement. Sur 429, attendre `Retry-After` sans relancer.

```js
// Liste de produits : FastAPI, repli sur Django (même enveloppe, sans facettes).
async function listerProduits(params, signal) {
  const query = new URLSearchParams(params).toString();
  try {
    const reponse = await fetch(`${FASTAPI_URL}/recherche/produits?${query}`, { signal });
    if (reponse.status !== 503) return await reponse.json(); // 200, 400, 404, 429 : réponse de FastAPI
  } catch (erreur) {
    if (erreur.name === 'AbortError') throw erreur; // requête remplacée par une plus récente
  }
  const { tri, ...reste } = params; // Django ne connaît pas tri=pertinence
  const django = new URLSearchParams(tri === 'pertinence' ? reste : params).toString();
  return (await fetch(`${DJANGO_API_URL}/catalogue/produits/?${django}`, { signal })).json();
}
```

En repli, Django cherche la phrase exacte avec accents (`karité` trouve, `karite` non) : afficher les résultats sans facettes.

### Scan QR des passeports

Contrat complet : [`MODULE_SCAN_QR.md`](./MODULE_SCAN_QR.md). **FastAPI décode, Django certifie.** Routes publiques (aucun jeton).

Le QR imprimé sur l'étiquette contient `url_verification_publique` (donnée par l'espace vendeur Django) : `FRONTEND_BASE_URL/qr/verifier/{code}`. Deux parcours mènent à la **même page** :

| Parcours | Étapes |
|---|---|
| Appareil photo du téléphone (hors application) | Le navigateur ouvre directement la page `/qr/verifier/:code` |
| Scanner intégré (caméra) ou saisie du code imprimé sous le QR | `POST /fast/qr/scan` avec `{"qr_data": <contenu brut>}` → 200 : ouvrir `url_verification_publique` (la page `/qr/verifier/:code`) |

La page `/qr/verifier/:code` appelle **Django** `GET /api/passeports/verifier/{code}/`, **sans en-tête `Authorization`** (route publique ; un jeton expiré donnerait 401). C'est cet appel qui certifie, compte et journalise le scan : **un seul appel par affichage**. Affichage selon `statut_passeport` (`valide`, `revoque`) et `disponible_a_la_vente` ([`MODULE_PASSEPORT_QR.md`](./MODULE_PASSEPORT_QR.md) § 4) ; 404 : « Ce code ne correspond à aucun passeport ANITCHE » ; 429 : attendre `Retry-After`.

**La page `/qr/verifier/:code` doit exister avant toute impression de QR**, et le domaine (`anitche.com` ou `anitche.ci`) doit être fixé avant : l'URL est figée dès l'impression.

Réponse 200 de `POST /fast/qr/scan` : `{"code_passeport": "PAS-2026-1A2B3C4D", "url_verification_publique": "https://anitche.com/qr/verifier/PAS-2026-1A2B3C4D"}`. Elle **n'atteste pas** que le passeport existe (c'est Django qui le dit). Espaces, tirets et minuscules sont tolérés dans un code saisi ; 512 caractères au plus.

Refus (`errors.code[0]`) :

| Code HTTP | Code | Réaction |
|---|---|---|
| 400 | `qr_non_anitche` | Avertir : « Ce QR ne renvoie pas vers ANITCHE : l'étiquette n'est peut-être pas authentique. » **Ne jamais ouvrir l'URL scannée** |
| 400 | `lien_non_passeport` | « Ce lien ANITCHE n'est pas un passeport produit. » |
| 400 | `code_passeport_invalide` | Message sous le champ de saisie (format `PAS-AAAA-XXXXXXXX`) |
| 400 | clé `qr_data` (sans `code`) | Saisie vide ou trop longue |
| 429 | — | Attendre `Retry-After` |
| 503 | — | Proposer de scanner avec l'appareil photo du téléphone (le parcours ne dépend pas de FastAPI) |

```js
// Scanner intégré ou saisie : FastAPI décode, puis la page de vérification appelle Django.
async function ouvrirPasseport(contenuBrut, naviguer) {
  const reponse = await fetch(`${FASTAPI_URL}/qr/scan`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ qr_data: contenuBrut }),
  });
  const corps = await reponse.json();
  if (reponse.ok) {
    // Même page que celle ouverte par l'appareil photo : un seul comptage, par Django.
    return naviguer(new URL(corps.url_verification_publique).pathname);
  }
  // Jamais afficher contenuBrut comme du HTML, jamais ouvrir une URL refusée.
  return { refus: corps.errors?.code?.[0] ?? (reponse.status === 400 ? 'saisie_invalide' : reponse.status) };
}

// Page /qr/verifier/:code : un seul appel par affichage, sans Authorization.
async function verifierPasseport(code) {
  const reponse = await fetch(`${DJANGO_API_URL}/passeports/verifier/${encodeURIComponent(code)}/`);
  return { statut: reponse.status, corps: await reponse.json() };
}
```

### Conseiller IA

Contrat complet : [`MODULE_IA.md`](./MODULE_IA.md). Routes **authentifiées** (tout rôle, jeton de Django). Aucune vraie IA n'est branchée aujourd'hui : un conseiller **par règles** (`source: "regles"`) répond avec les **vrais produits** du catalogue ; le contrat ne changera pas le jour où une IA sera ajoutée (`source: "ia"`).

| Écran | Appel | Limite |
|---|---|---|
| Conversation « conseiller shopping » | `POST /fast/ia/conseil` | 20/h par utilisateur |
| Bloc « sélection pour vous » (catégories, budget) | `POST /fast/ia/recommandations` | 120/h par utilisateur |
| Fiche d'un produit proposé | **Django** `GET /api/catalogue/produits/<slug>/` | — |

- **Historique tenu par le frontend** : le serveur ne garde aucune conversation. Envoyer à chaque appel les derniers échanges (**10 messages au plus**, le dernier du client), en renvoyant les réponses précédentes avec `role: "assistant"` (le texte de `reponse`). Au-delà de 10 : ne garder que les plus récents.
- **Corps** : `messages` (1 000 caractères au plus par message), `occasion` et `style` (texte libre, 60 caractères), `budget_max` (**entier** FCFA, 1 ou plus : ne pas envoyer 0 ni un décimal), `categories` (0 à 5 **slugs** de `GET /api/catalogue/categories/`). Aucun autre champ : un champ inconnu (ancien `utilisateur_id`, `categories_preferees`…) donne **400**.
- **Réponse** : `reponse` (message), `produits_suggeres` (4 au plus ; 8 pour `recommandations`), `conseils_style`, `source`. Chaque produit a **les champs d'un résultat de recherche** : réutiliser la carte produit, avec `justification` en plus. Liste vide possible (200) : afficher `reponse`, qui explique pourquoi (budget trop bas…).
- **Afficher tous les textes comme du texte brut** (jamais `innerHTML`) ; ils ne contiennent aucun lien.
- `source` : `"regles"` → libellé « Sélection automatique » ; `"ia"` → « Conseil IA » (affichage conseillé pour être transparent avec le client).

Erreurs (`errors.code[0]` quand il existe) :

| Code HTTP | Code | Réaction |
|---|---|---|
| 400 | clé du champ (`messages.0.contenu`, `budget_max`…) | Message sous le champ ; `detail` sinon |
| 401 | — | Rafraîchir le jeton puis rejouer (§ 4) |
| 413 | `corps_trop_volumineux` | Raccourcir l'historique |
| 429 | — | Attendre `Retry-After`, bouton désactivé |
| 503 | `conseiller_desactive` | **Masquer** l'entrée du conseiller (coupé par l'équipe) |
| 503 | `conseiller_indisponible` ou sans code | « Le conseiller est momentanément indisponible, réessayez plus tard. » |

```js
// Conversation : le frontend garde l'historique et le renvoie à chaque message.
async function demanderConseil(historique, texte, { occasion, style, budgetMax, categories } = {}, jeton) {
  const messages = [...historique, { role: 'user', contenu: texte }].slice(-10);
  const corps = { messages };
  if (occasion) corps.occasion = occasion;
  if (style) corps.style = style;
  if (Number.isInteger(budgetMax) && budgetMax > 0) corps.budget_max = budgetMax;
  if (categories?.length) corps.categories = categories.slice(0, 5);

  const reponse = await fetch(`${FASTAPI_URL}/ia/conseil`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${jeton}` },
    body: JSON.stringify(corps),
  });
  const donnees = await reponse.json();
  if (!reponse.ok) return { erreur: donnees.errors?.code?.[0] ?? reponse.status, detail: donnees.detail };
  return {
    historique: [...messages, { role: 'assistant', contenu: donnees.reponse }].slice(-10),
    produits: donnees.produits_suggeres, // carte produit de la recherche + justification
    conseils: donnees.conseils_style,
    libelle: donnees.source === 'ia' ? 'Conseil IA' : 'Sélection automatique',
  };
}
```

## 13. Frais vendeur (« Mes reversements »)

Règle de la plateforme depuis le **28 septembre 2026** ([`MODULE_PAIEMENTS.md`](./MODULE_PAIEMENTS.md) § 5) :

- **14 % du prix de l'article** (prix effectif, promotion comprise ; un coupon du client ne réduit jamais la part du vendeur) ;
- **plus un frais fixe par article** : **100 FCFA** si ce prix est inférieur ou égal à **3 000 FCFA**, **200 FCFA** au-delà ;
- montants **TVA incluse** : aucune TVA ne s'y ajoute. Écrire « TVA incluse » à côté des frais ; ne pas afficher de montant HT ni de TVA séparée (la décomposition n'existe pas encore : elle viendra avec les factures de commission).

Ce que voit le vendeur (`GET /api/paiements/vendeur/reversements/`) : les frais **figés à la commande**, ligne par ligne, et leur total sur le reversement. Exemple, 3 savons à 2 000 FCFA :

```json
{
  "nom_produit": "Savon noir", "quantite": 3, "prix_unitaire": "2000.00",
  "taux_commission": "14.00", "frais_fixe_unitaire": 100,
  "montant_commission": "840.00", "montant_frais_fixes": "300.00", "montant_net_vendeur": "4860.00"
}
```

- **Ne jamais recalculer les frais côté interface** : afficher ceux de l'API. Une commande passée avant le 28/09/2026 garde `"12.00"` et `200` ; une boutique peut avoir une offre propre (autre taux, autre frais fixe), prioritaire sur la règle de la plateforme.
- La commission est arrondie au franc sur le total de la ligne (un demi-franc au franc supérieur) : 2 999 FCFA → 420 FCFA de commission, pas 419,86.
- Le texte de la règle (page d'aide, inscription vendeur) : « Frais ANITCHE : 14 % du prix de l'article + 100 FCFA par article jusqu'à 3 000 FCFA (200 FCFA au-delà), TVA incluse. »
