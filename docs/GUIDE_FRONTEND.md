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

Second backend, pour le temps réel et les services rapides. Les modules 2 à 4 de la refonte FastAPI (recherche, QR, IA) ajouteront leurs routes ici. Aujourd'hui : **suivi GPS du livreur**, contrat complet dans [`MODULE_SUIVI_GPS.md`](./MODULE_SUIVI_GPS.md).

### Base et authentification

| | Dev | Prod |
|---|---|---|
| HTTP | `http://localhost:8001` (`VITE_FASTAPI_URL`) | `https://anitche.com/fast` |
| WebSocket | `ws://localhost:8001` | `wss://anitche.com/fast` |

- **HTTP** : le **même jeton `access` que Django** (§ 4), en-tête `Authorization: Bearer <access>`. Sur **401**, rafraîchir le jeton auprès de Django (§ 4) puis rejouer : l'intercepteur axios de Django convient.
- **Format d'erreur** : identique à Django (§ 5). Les refus du suivi portent un **code machine** : se fier à `errors.code[0]`, **jamais au texte** de `detail`.
- **429** : en-tête `Retry-After`, exposé par CORS (§ 7).

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
