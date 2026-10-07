# ANITCHE — frontend

Monorepo pnpm + Turborepo : quatre portails (`apps/client`, `apps/vendeur`, `apps/livreur`, `apps/admin`) et la configuration partagée (`packages/config`).

## Prérequis

- Node.js 22 ou plus (`.nvmrc` : 22) ;
- Corepack (fourni avec Node), qui installe la version de pnpm du champ `packageManager`.

## Démarrage

```sh
corepack enable
pnpm install
pnpm dev
```

| Portail | Port |
|---|---|
| client | 5173 |
| vendeur | 5174 |
| livreur | 5175 |
| admin | 5176 |

Chaque portail proxifie `/api` vers Django et `/fast` vers FastAPI (préfixe retiré). Les cibles se règlent avec `VITE_PROXY_DJANGO` et `VITE_PROXY_FASTAPI` (voir `.env.example`, à copier en `.env`).

## Vérifications

```sh
pnpm turbo run lint typecheck test build
```

Un portail n'importe jamais un autre portail (règle ESLint `no-restricted-imports`, vérifiée par `packages/config/eslint/frontieres.test.js`).
