FROM node:22-alpine

WORKDIR /app

RUN corepack enable

# Le cache d'installation ne dépend que du lockfile
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm fetch

COPY . .
RUN pnpm install --offline --frozen-lockfile

EXPOSE 5173

# Un seul portail en conteneur (client, port 5173) ; les autres se lancent sur l'hôte avec `pnpm dev`
CMD ["pnpm", "--filter", "@anitche/app-client", "dev", "--host", "0.0.0.0"]
