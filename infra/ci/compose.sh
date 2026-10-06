#!/usr/bin/env bash
# infra/ci/compose.sh
# docker compose sur la pile de production plus la surcharge de CI, avec
# l'environnement généré par le job. À lancer depuis la racine du dépôt.
# Variables : ENV_CI (fichier d'environnement), CI_CERT_DIR (certificat de
# test), COMPOSE_PROJECT_NAME (nom du projet, commun à tous les appels).
set -euo pipefail

: "${ENV_CI:?ENV_CI doit désigner le fichier de variables de CI}"

exec docker compose \
    -f infra/docker-compose.prod.yml \
    -f infra/ci/docker-compose.ci.yml \
    --env-file "$ENV_CI" "$@"
