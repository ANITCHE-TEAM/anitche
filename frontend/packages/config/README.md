# @anitche/config

Configuration partagée des portails et des paquets : TypeScript, ESLint (dont les frontières d'import), Vite et Vitest.

## Dans un portail (`apps/*`)

- `tsconfig.json` : `"extends": "@anitche/config/tsconfig/react-app.json"` ;
- `eslint.config.js` : `export default configApp({ nom: "client", racine: import.meta.dirname })`, avec `configApp` importé de `@anitche/config/eslint` ;
- `vite.config.ts` : `configVite({ mode, port })` de `@anitche/config/vite` (port fixe, proxy `/api` et `/fast`, tests jsdom).

## Dans un paquet (`packages/*`)

- `tsconfig.json` : `"extends": "@anitche/config/tsconfig/react-library.json"` (DOM et JSX) ou `library.json` (ES2023 seul, sans DOM : `window` y est une erreur de compilation, ce qui convient à `utils`) ;
- `eslint.config.js` : `export default configPaquet({ nom: "ui", racine: import.meta.dirname })`, avec `configPaquet` importé de `@anitche/config/eslint`. `nom` vaut `utils`, `ui`, `api-client` ou `auth` ; `react: false` retire les règles des hooks et les globales du navigateur (`utils`). Le dossier `src/generes` (types générés) est ignoré ;
- `vitest.config.ts` : `defineConfig({ test: testConfigPaquet({ environment: "jsdom" }) })` avec `testConfigPaquet` de `@anitche/config/vitest` ; `environment: "node"` pour un paquet sans DOM (ni jsdom ni Testing Library chargés).

Les paquets sont « source seule » : leur `exports` pointe vers `./src/*.ts(x)`, sans étape de build.

## Règles de frontières

Définies dans `eslint/frontieres.js` (`frontieres()` pour un portail, `frontieresPaquet()` pour un paquet), vérifiées par `eslint/frontieres.test.js`. Les fichiers concernés sont ceux de `src/**/*.{ts,tsx}`.

| Réf. | Portée | Règle | Mécanisme |
|---|---|---|---|
| — | Portail | n'importe jamais un autre portail | `no-restricted-imports` |
| — | Portail | `zustand` seulement dans `fonctionnalites/<module>/store.ts` | `no-restricted-imports` |
| — | Portail | `app`, `mises-en-page`, `composants`, `lib`, modules : sens des imports, module atteint par son `index` | `eslint-plugin-boundaries` |
| F1 | Portail | un paquet s'importe par son point d'entrée : ni `@anitche/*/src`, ni chemin vers `packages/` | `no-restricted-imports` |
| F2 | Portail | ni `openapi-fetch` ni `openapi-typescript` : passer par `@anitche/api-client` | `no-restricted-imports` |
| F3 | Portail | ni `fetch`, ni `XMLHttpRequest`, ni `WebSocket`, sauf dans `fonctionnalites/*/api.ts` et `lib/**` | `no-restricted-globals` |
| F4 | `utils` | ni React, ni autre paquet `@anitche/*`, ni `zustand`, ni TanStack Query, ni client OpenAPI | `no-restricted-imports` |
| F5 | `ui` | ni `api-client`, ni `auth`, ni `zustand`, ni `react-router`, ni TanStack Query, ni client OpenAPI | `no-restricted-imports` |
| F6 | `api-client` | ni `ui`, ni `auth`, ni `react`/`react-dom`, ni `zustand` | `no-restricted-imports` |
| F7 | tout paquet | aucun portail (`@anitche/app-*`, `**/apps/**`) ni fichier d'un autre paquet par chemin | `no-restricted-imports` |
| F8 | tout workspace | import hors du dossier du workspace, ou d'un paquet non déclaré dans son `package.json` | `turbo boundaries` (CI) |
| F9 | paquets | pas de cycle entre paquets | `turbo boundaries` (code 1) **et** F5/F6 |
| — | Portail et paquet | `dangerouslySetInnerHTML` interdit (attribut JSX et propriété d'objet) | `no-restricted-syntax` |

### À savoir

- **Sens des dépendances** : `utils` ← `api-client` ← `auth`, et `ui` ← `auth`. `auth` peut donc importer `ui` ; `ui` n'importe jamais `auth`. Aucune règle ne restreint les imports propres d'`auth` à part F7.
- **Cycle** : `turbo boundaries` échoue sur un cycle entre paquets (`Circular package dependency detected`, `1 issue found`, code de sortie 1). ESLint (F5, F6) bloque aussi l'import fautif, indépendamment de `package.json`. Les deux barrières sont actives.
- **F3 ne couvre pas `window.fetch`, `globalThis.fetch` ni `self.fetch`** : `no-restricted-globals` ne voit que l'identifiant global nu. C'est une limite connue, pas une autorisation.
- **F3 et les tests** : la règle s'applique aussi aux fichiers de test d'un portail. Un test qui doit appeler ou mocker `fetch` nu demandera une exception explicite (`eslint-disable` justifié ou entrée dédiée dans `frontieres()`) ; il n'y en a aucune aujourd'hui.
- **`dangerouslySetInnerHTML`** est interdit partout, portails **et** paquets. Rendre du texte, ou assainir en amont dans un paquet dédié.
- **`package.json` reste la première barrière** : pnpm ne résout que ce qui est déclaré. ESLint ajoute un message explicite et protège d'une dépendance déclarée par erreur.
- **Les gardes d'`auth` ne dépendent d'aucun routeur** : `ExigerConnexion`, `ExigerRole` et `RedirigerSiConnecte` reçoivent en props ce qu'il faut rendre (`attente`, `invite`, `refuse`, `connecte`). La redirection vers `/connexion` et la page 403 sont fournies par le portail.

## Voir une règle échouer

Chaque règle a un test automatique et une manipulation de 30 secondes. Les messages ci-dessous sont ceux **relevés** le 11 octobre 2026 (ESLint 10, turbo 2.11.7, openapi-typescript 7.13.0). Défaire chaque manipulation ensuite ; les fichiers cités existent, sauf mention contraire.

| # | Règle | Manipulation | Erreur attendue |
|---|---|---|---|
| 1 | F1 | dans `apps/client/src/app/App.tsx`, ajouter `import "@anitche/ui/src/components/button";`, puis `pnpm --filter @anitche/app-client lint` | `no-restricted-imports` : `'@anitche/ui/src/components/button' import is restricted from being used by a pattern. Importer le paquet par son point d'entrée (@anitche/<paquet>), jamais par ses fichiers ni par un chemin vers packages/` |
| 2 | F2 | `import createClient from "openapi-fetch";` dans le même fichier | `no-restricted-imports` : `'openapi-fetch' import is restricted from being used by a pattern. Un portail passe par @anitche/api-client : ni openapi-fetch ni openapi-typescript directement` |
| 3 | F3 | `fetch("/api/x");` dans `apps/client/src/app/App.tsx`, puis le même appel dans un fichier `apps/client/src/fonctionnalites/panier/api.ts` (à créer pour l'essai) | `no-restricted-globals` : `Unexpected use of 'fetch'. Aucun appel réseau dans un composant ou une page : passer par fonctionnalites/<module>/api.ts (ou lib/) et @anitche/api-client` ; **aucune** erreur dans `api.ts` |
| 4 | F4 | `import { useState } from "react";` dans `packages/utils/src/montant.ts`, puis `pnpm --filter @anitche/utils lint` | `no-restricted-imports` : `'react' import is restricted from being used by a pattern. utils reste pur : ni React, ni autre paquet, ni état, ni client HTTP` |
| 5 | F5 | `import { useSession } from "@anitche/auth";` dans `packages/ui/src/components/button.tsx`, puis `pnpm --filter @anitche/ui lint`. Variante : déclarer en plus `@anitche/auth` dans `ui/package.json` | `no-restricted-imports` : `'@anitche/auth' import is restricted from being used by a pattern. Sens des dépendances : utils ← api-client ← auth, et ui ← auth ; ui ne connaît ni api-client ni auth`. **Identique** avec la dépendance déclarée : la règle ne dépend pas de `package.json` |
| 6 | F6 | `import { Button } from "@anitche/ui/button";` dans `packages/api-client/src/clients.ts`, puis `pnpm --filter @anitche/api-client lint` | même message de sens des dépendances, sur `@anitche/ui/button` |
| 7 | F7 | `import x from "../../../apps/client/src/app/App";` dans `packages/auth/src/session.ts`, puis `pnpm --filter @anitche/auth lint` | `no-restricted-imports` : `'../../../apps/client/src/app/App' import is restricted from being used by a pattern. Un paquet n'importe jamais un portail : la chose partagée descend dans le paquet, pas l'inverse` |
| 8 | F8 | `import "../../ui/src/lib/cn";` dans `packages/utils/src/montant.ts`, puis `pnpm turbo boundaries` | `import ` + chemin + ` leaves the package` ; aide : `resolves to path ... which is outside of @anitche/utils` |
| 9 | F8 bis | importer `@anitche/ui` depuis `utils` **sans** le déclarer dans `package.json`, puis `pnpm turbo boundaries` | `cannot import package @anitche/ui because it is not a dependency` |
| 10 | F9 | déclarer `"@anitche/ui": "workspace:*"` dans `auth/package.json` **et** `"@anitche/auth": "workspace:*"` dans `ui/package.json` (les deux sens, sinon il n'y a pas de cycle), `pnpm install`, puis `pnpm turbo boundaries` | `WARNING Circular package dependency detected` puis `x Circular package dependency detected: @anitche/auth -> @anitche/ui -> @anitche/auth`, `1 issue found`, code de sortie 1. Un import `@anitche/auth` dans `ui` reste en plus refusé par ESLint (manipulation 5) |
| 11 | Types périmés | modifier une propriété dans `backend-django/schema.yaml`, puis `pnpm --filter @anitche/api-client verifier:api` | `✘ Generated types are not up-to-date!`, puis `ELIFECYCLE`, code 1. L'outil n'affiche pas la consigne de régénérer (`pnpm generer:api`) |

Les manipulations 5 (variante) et 10 modifient un `package.json` ; la 10 demande en plus un `pnpm install`, qui peut réécrire `pnpm-lock.yaml` : le restaurer ensuite (voir « Lockfile » dans [`../../README.md`](../../README.md)). Les cas testés automatiquement (imports acceptés et refusés) sont listés dans `eslint/frontieres.test.js`.
