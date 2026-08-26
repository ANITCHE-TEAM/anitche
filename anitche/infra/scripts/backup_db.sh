#!/usr/bin/env bash
# Sauvegarde la base PostgreSQL du conteneur "db" dans infra/backups/.
# À brancher sur une tâche cron pour des sauvegardes régulières, ex :
#   0 3 * * * /chemin/vers/anitche/infra/scripts/backup_db.sh
#
# Usage : ./infra/scripts/backup_db.sh

set -euo pipefail

cd "$(dirname "$0")/../.."   # racine du repo

BACKUP_DIR="infra/backups"
DATE=$(date +%Y-%m-%d_%H-%M-%S)
FILE="$BACKUP_DIR/anitche_$DATE.sql.gz"

mkdir -p "$BACKUP_DIR"

echo "==> Sauvegarde de la base en cours..."
docker compose -f infra/docker-compose.prod.yml exec -T db \
  pg_dump -U "${DB_USER:-postgres}" "${DB_NAME:-anitche}" | gzip > "$FILE"

echo "✅ Sauvegarde créée : $FILE"

# Ne garde que les 14 dernières sauvegardes
ls -1t "$BACKUP_DIR"/*.sql.gz | tail -n +15 | xargs -r rm --

echo "==> Sauvegardes conservées :"
ls -lh "$BACKUP_DIR"
