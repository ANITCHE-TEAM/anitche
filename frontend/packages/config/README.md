# @anitche/config

Configuration partagée des portails : TypeScript, ESLint (dont les frontières d'import), Vite et Vitest.

Dans un portail :

- `tsconfig.json` : `"extends": "@anitche/config/tsconfig/react-app.json"` ;
- `eslint.config.js` : `export default configApp({ nom: "client", racine: import.meta.dirname })`, avec `configApp` importé de `@anitche/config/eslint` ;
- `vite.config.ts` : `configVite({ mode, port })` de `@anitche/config/vite` (port fixe, proxy `/api` et `/fast`, tests jsdom).

Règles de frontières : `eslint/frontieres.js`, vérifiées par `eslint/frontieres.test.js`.
