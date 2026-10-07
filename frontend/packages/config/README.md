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

Définies dans `eslint/frontieres.js`, vérifiées par `eslint/frontieres.test.js`.

| Portée | Règle | Mécanisme |
|---|---|---|
| Portail | n'importe jamais un autre portail | `no-restricted-imports` |
| Portail | `zustand` seulement dans `fonctionnalites/<module>/store.ts` | `no-restricted-imports` |
| Portail | `app`, `mises-en-page`, `composants`, `lib`, modules : sens des imports, module atteint par son `index` | `eslint-plugin-boundaries` |
| Paquet | `utils` : ni React, ni autre paquet, ni `zustand`, ni TanStack Query, ni client OpenAPI | `no-restricted-imports` |
| Paquet | `ui` : ni `api-client`, ni `auth`, ni `zustand`, ni `react-router`, ni TanStack Query, ni client OpenAPI | `no-restricted-imports` |
| Paquet | `api-client` : ni `ui`, ni `auth`, ni `react`/`react-dom`, ni `zustand` | `no-restricted-imports` |
| Paquet | tout paquet : aucun portail, aucun fichier d'un autre paquet par chemin (`@anitche/<paquet>` seulement) | `no-restricted-imports` |

Sens des dépendances entre paquets : `utils` ← `api-client` ← `auth`, et `ui` ← `auth`. Turborepo **avertit** d'un cycle entre paquets (`Circular package dependency detected`) mais exécute quand même les tâches : seule la règle ESLint l'empêche.

### Voir une règle échouer

Dans un paquet, ajouter par exemple `import { useState } from "react";` à un fichier de `packages/utils/src/`, puis `pnpm --filter @anitche/utils lint` : l'erreur `no-restricted-imports` porte le message de la règle. Les cas testés (imports acceptés et refusés) sont listés dans `eslint/frontieres.test.js`.
