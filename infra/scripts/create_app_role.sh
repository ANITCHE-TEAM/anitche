#!/bin/bash
# infra/scripts/create_app_role.sh
# Crée (ou met à jour) le rôle PostgreSQL applicatif anitche_app, utilisé par
# Django et Celery, dans le conteneur "db" du compose de production, et lui
# donne la base. Idempotent. À lancer avant les migrations (deploy.sh).
#
# Usage, depuis n'importe quel dossier :
#   DB_PASSWORD=... infra/scripts/create_app_role.sh
#
# Le mot de passe est celui de DB_PASSWORD (infra/.env), transmis au
# conteneur par l'environnement, jamais en argument de commande. Le script se
# connecte avec le superutilisateur du conteneur (POSTGRES_USER), par le
# socket local : il ne sert qu'à ce script, à celui du rôle FastAPI et aux
# sauvegardes.
# Postgres managé (sans Docker) : lancer directement psql sur
# infra/postgres/app_role.sql (voir l'en-tête de ce fichier).

set -euo pipefail

: "${DB_PASSWORD:?DB_PASSWORD doit être défini}"
export DB_PASSWORD

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$INFRA_DIR/docker-compose.prod.yml}"

docker compose -f "$COMPOSE_FILE" exec -T -e DB_PASSWORD db \
    sh -c 'psql -X -q -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    < "$INFRA_DIR/postgres/app_role.sql"

echo "Rôle anitche_app prêt."
