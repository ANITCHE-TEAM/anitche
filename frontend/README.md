# ANITCHE — frontend

Monorepo pnpm + Turborepo : quatre portails (`apps/`) et cinq paquets (`packages/`).

| Dossier | Nom | Rôle |
|---|---|---|
| `apps/client` | `@anitche/app-client` | portail client (port 5173) |
| `apps/vendeur` | `@anitche/app-vendeur` | portail vendeur (5174) |
| `apps/livreur` | `@anitche/app-livreur` | portail livreur (5175) |
| `apps/admin` | `@anitche/app-admin` | portail admin (5176) |
| `packages/config` | `@anitche/config` | tsconfig, ESLint (dont les frontières d'import), Vite, Vitest ([README](./packages/config/README.md)) |
| `packages/utils` | `@anitche/utils` | montants FCFA, dates (Africa/Abidjan), téléphone, schémas Zod : sans React, sans réseau |
| `packages/api-client` | `@anitche/api-client` | types générés depuis les deux schémas OpenAPI, clients HTTP Django et FastAPI, `ErreurApi`, `QueryClient` |
| `packages/auth` | `@anitche/auth` | session, jetons, rafraîchissement concurrent, gardes (sans écran) |
| `packages/ui` | `@anitche/ui` | primitives (bouton, champ, etc.) et thème Tailwind provisoire |

Les paquets sont « source seule » : leur `exports` pointe vers `./src/*.ts(x)`, sans étape de build. Seuls les portails ont une tâche `build`.

## Prérequis

- Node.js 22 (`.nvmrc` : 22 ; `engines.node` : `>=22.13.0`) ;
- Corepack (fourni avec Node), qui installe la version de pnpm du champ `packageManager`.

## Démarrage

```sh
corepack enable
pnpm install
cp .env.example .env     # cibles du proxy de dev (valeurs par défaut : localhost:8000 et :8001)
pnpm dev                 # lance les quatre portails
```

Pour un seul portail :

```sh
pnpm --filter @anitche/app-client dev
```

Chaque portail proxifie `/api` vers Django (`VITE_PROXY_DJANGO`) et `/fast` vers FastAPI (`VITE_PROXY_FASTAPI`, préfixe `/fast` retiré, WebSocket compris). Les ports sont fixes (`strictPort`) : un port occupé fait échouer le démarrage au lieu d'en prendre un autre.

Avec Docker (`infra/docker-compose.yml`), seul le portail **client** tourne en conteneur (port 5173) ; les trois autres se lancent sur l'hôte avec `pnpm dev`.

## Commandes

À lancer depuis `frontend/`.

| Commande | Effet |
|---|---|
| `pnpm dev` | `turbo run dev` : serveurs Vite des portails |
| `pnpm lint` | `turbo run lint` : ESLint de chaque workspace |
| `pnpm typecheck` | `turbo run typecheck` : `tsc --noEmit` de chaque workspace |
| `pnpm test` | `turbo run test` : Vitest de chaque workspace |
| `pnpm build` | `turbo run build` : build des portails |
| `pnpm turbo run lint typecheck test build` | l'enchaînement de la CI |
| `pnpm turbo boundaries` | contrôle Turborepo : import hors du dossier du paquet, paquet non déclaré, cycle entre paquets (code de sortie 1). Commande expérimentale côté Turborepo |
| `pnpm generer:api` | régénère les types depuis les schémas OpenAPI |
| `pnpm verifier:api` | échoue si les types commités sont périmés |
| `pnpm --filter <nom> <script>` | un script d'un seul workspace (`pnpm --filter @anitche/utils test`) |

### Types de l'API : `generer:api` et `verifier:api`

Ces deux scripts n'existent que dans `packages/api-client` ; les commandes racine les y relaient.

```text
generer:api   openapi-typescript ../../../backend-django/schema.yaml  -o src/generes/django.ts
              openapi-typescript ../../../backend-fastapi/openapi.json -o src/generes/fastapi.ts
verifier:api  les mêmes commandes avec --check
```

- Les sources sont `backend-django/schema.yaml` et `backend-fastapi/openapi.json`, versionnés ; aucun serveur n'a besoin de tourner.
- `src/generes/*.ts` est commité et ne s'édite jamais à la main.
- Les chemins sont **relatifs** : un chemin absolu contenant un espace est encodé en `%20` par la CLI et la fait échouer.
- Ces tâches ne sont pas mises en cache par Turborepo (`"cache": false`) : les sources sont hors de `frontend/`.
- La CI exécute `verifier:api` : une PR qui modifie `schema.yaml` ou `openapi.json` sans régénérer les types fait échouer `ci-frontend`. Régénérer dans la même PR. Si les types sont périmés, la commande affiche `Generated types are not up-to-date!` et sort en code 1 ; elle n'indique pas la commande à lancer (`pnpm generer:api`).

## Frontières d'import

Quatre règles à connaître au quotidien ; le détail et la manière de les voir échouer sont dans [`packages/config/README.md`](./packages/config/README.md).

- Un portail n'importe jamais un autre portail ni un fichier d'un paquet : uniquement `@anitche/<paquet>` ou un sous-chemin déclaré dans `exports` (`@anitche/ui/button`).
- Un portail n'appelle pas `openapi-fetch` : il passe par `@anitche/api-client`.
- Aucun `fetch`, `XMLHttpRequest` ni `WebSocket` dans un portail hors `fonctionnalites/<module>/api.ts` et `lib/`. La règle ne voit pas `window.fetch`, `globalThis.fetch` ni `self.fetch` (limite connue) : ne pas la contourner par là.
- `dangerouslySetInnerHTML` est interdit dans les portails et dans les paquets.

Sens des dépendances entre paquets : `utils` ← `api-client` ← `auth`, et `ui` ← `auth`. Donc `auth` peut utiliser `ui`, mais `ui` n'importe jamais `auth`. Les dépendances réellement déclarées aujourd'hui : `auth` → `api-client` ; `client` → `ui`.

## Tests

Vitest partout (`globals: false` : importer `describe`, `it`, `expect`).

| Workspace | Environnement | Remarque |
|---|---|---|
| `utils` | `node` | process lancé avec `TZ=America/New_York` pour prouver l'indépendance au fuseau |
| `api-client` | `node` | MSW (`setupServer`, `onUnhandledFrame: "error"`) : une requête sans handler fait échouer le test |
| `auth`, `ui`, portails | `jsdom` + Testing Library | |
| `config` | `node` | tests des règles de frontières : `eslint/frontieres.test.js` |

## Styles

Tailwind v4 n'est branché que dans `apps/client` (plugin `@tailwindcss/vite`, `src/styles/index.css`). Les trois autres portails le recevront quand leur étape commencera. Le thème de `@anitche/ui` (`src/styles/theme.css`) est **provisoire**, en attendant la maquette : ne changer que les valeurs de `:root`, les composants ne lisent que les noms `--anitche-*`.

Chaque portail qui utilise `@anitche/ui` déclare dans son CSS `@source "../../../../packages/ui/src"`, sans quoi les classes des composants de `ui` ne sont jamais générées (le lien pnpm passe par `node_modules`).

## Lockfile

`pnpm-lock.yaml` n'est pas canonique : un `pnpm install` **sans** `--frozen-lockfile` re-résout `supports-color` et le peer `msw` de `vitest`, et réécrit le fichier. La CI et le `Dockerfile` de dev utilisent `--frozen-lockfile`, donc cela ne bloque rien ; en local, ne pas commiter un lockfile modifié par ce seul effet.

## Production

`Dockerfile.prod` n'a pas été migré vers pnpm : il copie `package*.json`, lance `npm ci` et `npm run build` sous Node 20, ce qui ne correspond plus au monorepo. À reprendre avant de construire une image de production du frontend.
