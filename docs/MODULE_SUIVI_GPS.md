# Module suivi GPS — contrat et règles

> Périmètre : backend FastAPI, `backend-fastapi/app/` (routeur `routeurs/suivi_temps_reel.py`, services `delivery_access.py`, `tracking.py`, `eta.py`).
> Le statut de la livraison et les droits restent ceux de Django ([`MODULE_LIVRAISON.md`](./MODULE_LIVRAISON.md)) : FastAPI les **lit**, ne les modifie jamais.

## 1. Ce sur quoi le module s'appuie

| Dépendance | Usage |
|---|---|
| Django (`/api/utilisateurs/jeton/verification/`) | Jeton → `{id, role}` (socle commun `app/core/auth.py`, cache Redis de 30 s) |
| PostgreSQL (rôle `anitche_fastapi_ro`) | Une requête par action : statut, livreur assigné, client de la commande, rôle et compte actif **en base**, point GPS du client. Droits **par colonne** (§ 6) |
| Redis (base 2) | Dernière position (TTL 120 s), diffusion pub/sub entre workers et instances, limites de débit |

Aucune écriture dans PostgreSQL : **pas d'historique des positions**.

## 2. Architecture

```text
routeurs/suivi_temps_reel.py  HTTP et WebSocket : validation, codes de réponse et de fermeture
services/delivery_access.py   requête SQL unique + règles pures can_publish / can_listen
services/tracking.py          Redis : dernière position, publication (SET + PUBLISH),
                              TrackingHub (un abonnement pub/sub par processus, lifespan)
services/eta.py               distance et temps restants, indicatifs (fonctions pures)
modeles/suivi_temps_reel.py   corps, réponses et messages WebSocket (Pydantic)
```

## 3. Droits (calqués sur Django)

| Action | Qui | Statut |
|---|---|---|
| **Publier** (`POST /livraison/position`) | Le **livreur assigné** seulement : rôle `livreur` **en base**, compte actif, `livraison.livreur_id` = utilisateur du jeton. L'administration ne publie pas (comme dans Django, elle ne fait pas les étapes du livreur) | `en_cours` |
| **Écouter** (`GET`, WebSocket) | Même périmètre que `livraisons_visibles`, dans le même ordre : `admin` / `super_admin` : toutes ; `livreur` : celles qui lui sont assignées ; tout autre rôle (client, vendeur, modérateur, support) : celles de **ses propres commandes**. Compte actif en base | `en_cours` |

- Le **vendeur n'écoute pas** les livraisons de sa boutique (Django ne lui donne ni adresse ni suivi), seulement celles de ses propres achats.
- Le contrôle « jamais le propriétaire de la boutique » n'est pas repris : Django le garantit à l'assignation.
- Rôle et compte actif sont lus **en base** à chaque action : le retrait d'un livreur (rôle repassé à `client`) a un effet immédiat, même si son jeton est encore en cache.
- Hors `en_cours` : aucune position publiée, lue ni conservée (`expediee` : le livreur est chez le vendeur ; `livree`, `echouee`, `annulee` : fin de la tournée).

## 4. Contrat HTTP

Base : dev `http://localhost:8001`, prod `https://anitche.com/fast`. En-tête `Authorization: Bearer <access>` (jeton Django). Format d'erreur commun ([`GUIDE_FRONTEND.md`](./GUIDE_FRONTEND.md) § 5). Chaque refus métier porte un **code machine** dans `errors.code[0]` : le frontend s'y fie, jamais au texte de `detail`.

### `POST /livraison/position` (livreur)

```json
{ "livraison_id": "d27b428a-ed14-4284-bc44-de11f3552bd5", "latitude": 5.32, "longitude": -4.015, "vitesse_kmh": 28.0, "cap_degres": 40.0 }
```

- `livraison_id` : UUID ; `latitude` [-90, 90] ; `longitude` [-180, 180] ; `vitesse_kmh` facultatif [0, 200] ; `cap_degres` facultatif [0, 360[. `NaN` et `Infinity` refusés.
- Le livreur est l'utilisateur du jeton, l'horodatage est celui du **serveur** : `livreur_id`, `horodatage` et tout champ inconnu sont **ignorés**.
- **200** : la position publiée (même format que le `GET`).

### `GET /livraison/position/{livraison_id}` (client, livreur assigné, administration)

**200** :

```json
{
  "livraison_id": "d27b428a-ed14-4284-bc44-de11f3552bd5",
  "latitude": 5.32,
  "longitude": -4.015,
  "vitesse_kmh": 28.0,
  "cap_degres": 40.0,
  "horodatage": "2026-09-27T23:28:12.251Z",
  "distance_restante_km": 12.8,
  "temps_estime_minutes": 39
}
```

- `horodatage` : heure du serveur à la réception, UTC, ISO 8601.
- `vitesse_kmh`, `cap_degres` : nombres ou `null`.
- `distance_restante_km`, `temps_estime_minutes` : **indicatifs** (§ 5), `null` si le client n'a pas donné son point GPS au checkout.
- **Jamais** l'identifiant du livreur (le client connaît déjà son prénom et son téléphone par Django pendant `en_cours`).

### Réponses d'erreur

| Code | `errors.code[0]` | Cas |
|---|---|---|
| 400 | — (`errors.<champ>`) | Validation : `errors.livraison_id` (« Doit être un UUID valide. »), `errors.latitude`… |
| 401 | — | Jeton absent, invalide ou expiré : rafraîchir le jeton |
| 403 | `acces_reserve_livreurs` | Publication par un compte qui n'est pas livreur (administration comprise), livreur retiré ou désactivé |
| 403 | `livraison_non_assignee` | Publication par un livreur qui n'est pas l'assigné |
| 404 | `livraison_introuvable` | Livraison inexistante, ou hors du périmètre du compte (sans révéler l'existence, comme Django) |
| 404 | `aucune_position` | `GET` : livraison visible mais pas `en_cours`, jamais publiée, position expirée (> 120 s) ou publiée par un ancien livreur. **Rien à afficher, ce n'est pas une panne** |
| 409 | `livraison_pas_en_cours` | Publication hors `en_cours` : le livreur arrête d'émettre |
| 429 | — | `gps_publish` 1 500/h, `gps_read` 720/h par compte ; en-tête `Retry-After` |
| 503 | — | PostgreSQL, Redis ou Django indisponible |

## 5. Distance et temps restants : INDICATIFS

- Calculés **seulement si le client a donné son point GPS** au checkout (`GroupeCommande.livraison_latitude/longitude`, [`MODULE_COMMANDES.md`](./MODULE_COMMANDES.md) § 3 bis). Sans point : `null`, jamais une valeur inventée (ni centre de commune, ni destination par défaut).
- Distance : vol d'oiseau (Haversine) × **facteur de détour 1,4** (la route est plus longue que la ligne droite ; 1,3 à 1,5 en ville, hypothèse à mesurer), arrondie à 0,1 km.
- Temps : distance ÷ **20 km/h** (vitesse moyenne urbaine d'une moto ou d'une voiture à Abidjan, arrêts compris, hypothèse à mesurer), en minutes entières arrondies au supérieur, **1 minute au moins**.
- **Sans trafic en temps réel ni itinéraire** : afficher « environ 39 min », jamais une heure d'arrivée précise.
- Réglages : `ETA_DETOUR_FACTOR`, `ETA_AVERAGE_SPEED_KMH` (`app/core/settings.py`).

## 6. Données : Redis et PostgreSQL

| Élément | Règle |
|---|---|
| Dernière position | Clé `fastapi:gps:v1:pos:<livraison>` (JSON avec le livreur qui l'a publiée), **TTL 120 s** (`TRACKING_POSITION_TTL`) |
| Diffusion | Canal `fastapi:gps:v1:chan:<livraison>`, message **sans** l'identifiant du livreur. `SET` et `PUBLISH` dans une même transaction Redis |
| Fin de livraison | Aucune écriture hors `en_cours` (409). À chaque lecture (`GET`, connexion, revalidation), si la livraison n'est plus `en_cours` ou si la position vient d'un autre livreur que l'assigné actuel : **suppression** de la clé. Sinon, expiration en 120 s |
| Redis indisponible | 503 (HTTP), 1011 (WebSocket). Aucun repli en mémoire |
| Droits PostgreSQL | `infra/postgres/fastapi_readonly.sql` : `SELECT` sur `livraison_livraison (id, commande_id, livreur_id, status)`, `commandes_commande (id, client_id, groupe_id)`, `commandes_groupecommande (id, livraison_latitude, livraison_longitude)`, `utilisateurs_utilisateur (id, role, is_active)`. Rien d'autre (ni code de livraison, ni adresse texte, ni email, ni mot de passe, ni montants) |

**Déploiement** : relancer `infra/scripts/create_fastapi_readonly_role.sh` **après** `migrate` (le script échoue si une table manque). Une colonne renommée par Django casse la requête : le job CI `integration` le détecte.

Concurrence acceptée : si Django passe la livraison à `livree` entre la lecture du statut et l'écriture de la position, une dernière position peut être écrite ; elle n'est jamais lue (lecture conditionnée au statut) et expire en 120 s. Aucun argent n'en dépend.

## 7. WebSocket `/livraison/ws/{livraison_id}`

Base : `ws://localhost:8001` (dev), `wss://anitche.com/fast` (prod).

### Protocole

1. Ouvrir la connexion **seulement** si la livraison est `en_cours` (lue dans Django). Le jeton ne passe **jamais** par l'URL (`?token=` est ignoré).
2. Envoyer aussitôt `{"type": "auth", "token": "<access>"}` : **5 s** au plus (`WS_AUTH_TIMEOUT`).
3. Contrôles : jeton (Django), limite `ws_connect` (60/h par compte), droits et statut (§ 3).
4. Messages reçus :

| `type` | Contenu | Quand |
|---|---|---|
| `authentifie` | `{"type": "authentifie", "livraison_id"}` | Après les contrôles |
| `position` | Format du `GET` (§ 4) + `"type": "position"` | Dernière position à la connexion (si elle existe), puis chaque publication |
| `fin_suivi` | `{"type": "fin_suivi", "livraison_id", "statut": "livree"}` | La livraison n'est plus `en_cours`, juste avant la fermeture 1000 |

5. Renouvellement du jeton : renvoyer `{"type": "auth", "token": "<nouveau access>"}` **avant son expiration** (15 min). Même compte obligatoire. Pas de réponse en cas de succès ; échec : fermeture 4401. C'est le **seul** message accepté après l'authentification, 6 par minute au plus.
6. Revalidation par le serveur toutes les **60 s** (`WS_REVALIDATE_INTERVAL`) : jeton et droits. Un jeton révoqué ou expiré coupe la connexion en 90 s au plus (60 s + cache de 30 s).

Doublons possibles juste après la connexion (abonnement avant la lecture de la dernière position, pour n'en perdre aucune) : garder la position la plus récente selon `horodatage`. Un client lent perd les positions intermédiaires (file de 8 par connexion), jamais la dernière.

### Codes de fermeture

| Code | Cas | Réaction du frontend |
|---|---|---|
| 1000 | Fin normale : la livraison n'est plus `en_cours` (après `fin_suivi`) | Ne pas se reconnecter ; relire la livraison dans Django |
| 1008 | Identifiant non UUID, message non prévu ou binaire après l'authentification | Bug du client : ne pas se reconnecter en boucle |
| 1009 | Message de plus de 4 096 octets | Idem |
| 1011 | PostgreSQL, Redis ou Django indisponible, abonnement pub/sub perdu, erreur interne | Reconnexion avec délai croissant (1, 2, 5, 10 s…) |
| 4401 | Pas d'authentification en 5 s, premier message invalide, jeton refusé (connexion, renouvellement, revalidation), jeton d'un autre compte | Rafraîchir le jeton puis se reconnecter |
| 4403 | Hors périmètre ou pas `en_cours` à la connexion ; droits perdus en cours de route (réassignation, compte désactivé) | Ne pas se reconnecter ; relire la livraison dans Django |
| 4429 | `ws_connect` dépassé, Django en 429, plus de 6 messages par minute | Attendre (quelques minutes) avant de se reconnecter |
| 1006 | Coupure réseau (pas envoyé par le serveur) | Reconnexion avec délai croissant |

Motif (`reason`) : phrase française courte, à ne pas afficher ni analyser : se fier au **code**. Repli possible : `GET` toutes les 5 s (720/h).

Côté serveur : aucun écho, messages construits par Pydantic (JSON strict, sans `NaN`), `--ws-max-size 8192` et ping toutes les 20 s (`--ws-ping-interval 20 --ws-ping-timeout 20`) dans les composes et le Dockerfile. La chaîne de requête de la poignée de main est retirée du journal d'uvicorn.

## 8. Rythme conseillé

- **Livreur** : une position toutes les **5 s** pendant `en_cours` (720/h, sous la limite de 1 500/h). Arrêt sur **403, 404 ou 409**. Sur 401 : rafraîchir le jeton. Sur 429 : attendre `Retry-After`.
- **Client, administration** : le WebSocket ; `GET` toutes les 5 s en repli.

## 9. Réglages (`app/core/settings.py`)

| Variable | Défaut | Rôle |
|---|---|---|
| `TRACKING_POSITION_TTL` | 120 | Durée de vie de la dernière position (s) |
| `WS_AUTH_TIMEOUT` | 5 | Délai du premier message `auth` (s) |
| `WS_REVALIDATE_INTERVAL` | 60 | Revalidation du jeton et des droits (s) |
| `WS_MAX_MESSAGE_BYTES` | 4096 | Taille maximale d'un message reçu (≤ 8192, limite d'uvicorn) |
| `WS_CLIENT_MESSAGES_PER_MINUTE` | 6 | Messages du client par minute et par connexion |
| `TRACKING_QUEUE_SIZE` | 8 | Positions en attente par connexion |
| `ETA_DETOUR_FACTOR` | 1.4 | Facteur de détour (§ 5) |
| `ETA_AVERAGE_SPEED_KMH` | 20 | Vitesse moyenne urbaine (§ 5) |
| `RATE_LIMITS` | `gps_publish` 1500/hour, `gps_read` 720/hour, `ws_connect` 60/hour | Limites par compte |

## 10. Sécurité — risques couverts

| Risque | Protection |
|---|---|
| Position d'une livraison lue par un compte sans lien avec elle | Périmètre de `livraisons_visibles` (§ 3), 404 / 4403 hors périmètre |
| `livraison_id` en texte libre (`../admin`…) | UUID validé (400, 1008) avant toute requête ou clé Redis |
| `livreur_id` pris dans le corps | Livreur = utilisateur du jeton, livreur assigné lu en base |
| Publication hors livraison en cours | `en_cours` exigé (409 / `aucune_position` / 4403) |
| Administration publiant au nom d'un livreur | Seuls les livreurs publient (403 pour l'administration) |
| Fausse position ou fausse ETA par défaut | 404 `aucune_position` ; ETA seulement avec le point du client |
| État en mémoire, faux avec plusieurs workers | Redis (TTL, pub/sub), aucun singleton de module |
| Jeton WebSocket dans l'URL (journaux) | Authentification par premier message ; chaîne de requête retirée du journal d'uvicorn |
| Connexion restée ouverte après révocation ou réassignation | Revalidation toutes les 60 s, renouvellement du jeton par message |
| Écho de messages, messages sans limite de taille | Aucun écho, 1008 / 1009, 4 096 octets, `--ws-max-size 8192` |
| `NaN` / `Infinity` diffusés | Refusés (400), valeurs bornées |
| Lecture sans limite | `gps_read` 720/h, 6 messages client par minute |
| Identifiant du livreur envoyé au client | Jamais envoyé (stocké seulement pour détecter une réassignation) |
| Vendeur lisant toute livraison | Même règle que Django : ses achats seulement |
| Horodatage fourni par le client | Horodatage serveur, UTC, ISO 8601 |
| Diffusion bloquée par un abonné lent | `PUBLISH` Redis ; un abonné lent ne ralentit ni le livreur ni les autres |
| Erreurs WebSocket silencieuses | Journalisées (sans jeton), fermeture 1011, désabonnement garanti |

## 11. Tests

- `tests/test_suivi_gps.py` : un test (au moins) par risque du § 10, ETA dans les trois sorties, pannes (Redis, PostgreSQL, pub/sub perdu), abonnement unique par processus et nettoyage.
- `tests/test_suivi_gps_regles.py` : table rôle × lien × statut de `can_listen` / `can_publish`, ETA (références, `null` sans point, jamais `NaN`).
- `tests/integration/` (marqueur `integration`) : **vrais** PostgreSQL et Redis. Droits par colonne exacts (`information_schema.column_privileges`), colonnes refusées, écriture refusée, requête d'accès sur le schéma des migrations Django, pub/sub entre deux applications, parcours complet jusqu'à `fin_suivi`.
- CI (`.github/workflows/ci-fastapi.yml`) : job `test` sans service (`-m "not integration"`), job `integration` (Postgres 16, Redis 7, `migrate`, vrai script du rôle, `REQUIRE_INTEGRATION=1` : un test sauté fait échouer le job).

### Contrat OpenAPI versionné (tout le service FastAPI)

`backend-fastapi/openapi.json` est le schéma de **toutes** les routes `/fast` (recherche, IA, QR, suivi GPS, `/health`). `/openapi.json` n'existe pas en production : ce fichier est la référence du frontend, comme `backend-django/schema.yaml` pour `/api`.

```bash
cd backend-fastapi
python scripts/exporter_openapi.py           # régénère le fichier
python scripts/exporter_openapi.py --check   # échoue s'il n'est pas à jour, et donne la commande
```

- Le fichier est identique d'un poste à l'autre : le script construit l'application avec des réglages figés (`environment="test"`, valeurs par défaut du code) et **ne lit ni variable d'environnement ni `.env`**. JSON stable : indentation 2, sans échappement ASCII, `\n` final, ordre des clés conservé (celui des routes).
- Les chemins sont **sans le préfixe nginx** `/fast` et le schéma n'a pas de `servers` : la base de l'URL appartient au client (`GUIDE_FRONTEND.md` § 12, « Base et authentification »). Le WebSocket n'est pas décrit par OpenAPI : son contrat est le § 7.
- À régénérer et à versionner dans le même commit que tout changement de route, de modèle de réponse ou de description. `tests/test_openapi_versionne.py` (fichier à jour, indépendance de l'environnement, message de `--check`) et l'étape « Schéma à jour » de `ci-fastapi.yml` font échouer un oubli.
- Changer `app_version` ou `app_name` (`app/core/settings.py`) change le fichier : le régénérer.

En local, jamais sur la base de dev `anitche` (refusé par les fixtures) :

```bash
# base séparée, migrée par le conteneur Django, puis rôle
docker compose -f infra/docker-compose.yml exec db psql -U postgres -c "CREATE DATABASE anitche_fastapi_test"
docker compose -f infra/docker-compose.yml exec -e DB_NAME=anitche_fastapi_test backend-django python manage.py migrate --noinput
docker compose -f infra/docker-compose.yml exec -T -e FASTAPI_DB_PASSWORD=fastapi_ro_dev db \
    psql -X -v ON_ERROR_STOP=1 -U postgres -d anitche_fastapi_test < infra/postgres/fastapi_readonly.sql

cd backend-fastapi
FASTAPI_TEST_DATABASE_URL=postgresql://anitche_fastapi_ro:fastapi_ro_dev@localhost:5432/anitche_fastapi_test \
FASTAPI_TEST_ADMIN_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/anitche_fastapi_test \
FASTAPI_TEST_REDIS_URL=redis://localhost:6379/15 \
python -m pytest -m integration
```

## 12. Hors périmètre (étape hébergement)

nginx n'est pas modifié par ce module : `map $http_upgrade $connection_upgrade`, `location /fast/livraison/ws/` dédiée (`proxy_read_timeout 120s`, `proxy_buffering off`), `limit_conn` / `limit_req` sur la poignée de main, journal `$uri` plutôt que `$request`. Le ping de 20 s d'uvicorn tient déjà la connexion sous les 60 s d'inactivité de nginx et les 100 s de Cloudflare.

## 13. Justification des choix

- **Une requête SQL, règles en Python pur** : un seul aller-retour par action (clés primaires, moins d'une milliseconde), règles testées sans base, dans l'ordre de Django.
- **Un abonnement pub/sub par processus** plutôt qu'un par WebSocket : une connexion Redis par worker ; un client lent ne ralentit personne (file bornée, la plus ancienne position est jetée : seule la dernière compte).
- **Pas d'historique** : demande explicite ; la position est une donnée personnelle éphémère.
- **Pas de récepteur de signal Django** qui supprimerait la position dès la fin de la livraison : Django écrirait dans la base Redis de FastAPI (couplage). La fermeture intervient en 60 s au plus.
- **ETA sans centre de commune** : une commune d'Abidjan s'étend sur plusieurs kilomètres ; une durée fausse serait le même défaut qu'une fausse position par défaut (§ 10).
