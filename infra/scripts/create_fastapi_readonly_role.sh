#!/bin/bash
# infra/scripts/create_fastapi_readonly_role.sh
# Crée (ou met à jour) le rôle PostgreSQL en lecture seule du service
# FastAPI, anitche_fastapi_ro, dans le conteneur "db" du compose. Idempotent.
#
# Usage, depuis n'importe quel dossier :
#   FASTAPI_DB_PASSWORD=... infra/scripts/create_fastapi_readonly_role.sh
#   FASTAPI_DB_PASSWORD=... COMPOSE_FILE=infra/docker-compose.prod.yml infra/scripts/create_fastapi_readonly_role.sh
#
# Le mot de passe doit être celui de DATABASE_URL (FASTAPI_DB_PASSWORD du
# compose ; en dev, la valeur par défaut est fastapi_ro_dev). Il est transmis
# au conteneur par l'environnement, jamais en argument de commande.
# Postgres managé (sans Docker) : lancer directement psql sur
# infra/postgres/fastapi_readonly.sql (voir l'en-tête de ce fichier).

set -euo pipefail

: "${FASTAPI_DB_PASSWORD:?FASTAPI_DB_PASSWORD doit être défini}"
export FASTAPI_DB_PASSWORD

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$INFRA_DIR/docker-compose.yml}"

docker compose -f "$COMPOSE_FILE" exec -T -e FASTAPI_DB_PASSWORD db \
    sh -c 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    < "$INFRA_DIR/postgres/fastapi_readonly.sql"

echo "Rôle anitche_fastapi_ro prêt."
