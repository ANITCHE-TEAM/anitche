#!/usr/bin/env bash
# infra/scripts/migrer.sh
# Seule façon d'appliquer les migrations Django en production (deploy.sh
# l'appelle ; la commande du conteneur backend-django ne migre jamais).
#
# Étapes, arrêt à la première erreur :
#   1. compte les migrations en attente (conteneur ponctuel) ; aucune : fin,
#      sans sauvegarde ni migration ;
#   2. sauvegarde de la base et des médias (infra/scripts/backup.sh). Un
#      échec arrête tout, AUCUNE migration n'est appliquée : une migration
#      ratée se rattrape alors depuis une sauvegarde de quelques minutes, pas
#      de la nuit précédente ;
#   3. migrations (conteneur ponctuel).
#
# Le service db doit tourner (deploy.sh le démarre avant) et le rôle
# anitche_app exister (create_app_role.sh). Le dépôt restic doit être
# initialisé (docs/SAUVEGARDES.md) : sans lui, la sauvegarde échoue et le
# déploiement s'arrête, premier déploiement compris.
#
# Variables : ENV_FILE (fichier d'environnement, infra/.env par défaut),
# COMPOSE_FILE (compose de production par défaut), transmises à backup.sh.
#
# Usage : infra/scripts/migrer.sh

set -euo pipefail

cd "$(dirname "$0")/../.."   # racine du dépôt

export ENV_FILE="${ENV_FILE:-infra/.env}"
export COMPOSE_FILE="${COMPOSE_FILE:-infra/docker-compose.prod.yml}"
compose() { docker compose --env-file "$ENV_FILE" "$@"; }

if [ ! -f "$ENV_FILE" ]; then
    echo "❌ $ENV_FILE introuvable." >&2
    exit 1
fi

echo "==> Migrations en attente"
# Même calcul que « migrate --check », mais une erreur (base injoignable,
# code cassé) se distingue de « des migrations attendent » : seule une ligne
# EN_ATTENTE <n> fait continuer.
sortie="$(compose run --rm --no-deps -T backend-django python manage.py shell -c "
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
executeur = MigrationExecutor(connection)
print('EN_ATTENTE', len(executeur.migration_plan(executeur.loader.graph.leaf_nodes())))
")" || { echo "❌ Impossible de lister les migrations en attente : rien n'est appliqué." >&2; exit 1; }
en_attente="$(sed -n 's/^EN_ATTENTE \([0-9][0-9]*\)$/\1/p' <<<"$sortie" | tail -n 1)"
if [ -z "$en_attente" ]; then
    echo "❌ Nombre de migrations en attente illisible : rien n'est appliqué." >&2
    exit 1
fi
if [ "$en_attente" = 0 ]; then
    echo "Aucune migration en attente : ni sauvegarde ni migration."
    exit 0
fi

echo "==> $en_attente migration(s) en attente : sauvegarde avant de migrer"
if ! bash infra/scripts/backup.sh; then
    echo "❌ Sauvegarde échouée : migrations NON appliquées, déploiement arrêté (journal : infra/backups/backup.log)." >&2
    exit 1
fi

echo "==> Migrations (conteneur ponctuel)"
compose run --rm --no-deps -T backend-django python manage.py migrate --noinput

echo "✅ $en_attente migration(s) appliquée(s), après sauvegarde."
