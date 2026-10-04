#!/usr/bin/env bash
# Script de déploiement simple pour un VPS (Hetzner, DigitalOcean, etc.)
# Suppose que le repo est déjà cloné sur le serveur et qu'un fichier
# infra/.env (non commité, droits 600) contient les secrets de prod.
#
# Étapes, arrêt à la première erreur :
#   1. code à jour (branche main) ;
#   2. contrôle des variables (check_prod_env.sh) ;
#   3. construction des images ;
#   4. démarrage de db et redis seuls, jusqu'à ce qu'ils soient prêts ;
#   5. migrations, dans un conteneur ponctuel ;
#   6. rôle PostgreSQL en lecture seule de FastAPI, recréé à chaque
#      déploiement : ses droits portent sur des tables et des vues créées
#      par les migrations (infra/postgres/fastapi_readonly.sql) ;
#   7. démarrage de toute la pile, puis statut.
#
# Usage : ./infra/scripts/deploy.sh

set -euo pipefail

cd "$(dirname "$0")/../.."   # se placer à la racine du repo

COMPOSE_PROD=infra/docker-compose.prod.yml
compose() { docker compose -f "$COMPOSE_PROD" --env-file infra/.env "$@"; }

echo "==> Récupération de la dernière version du code"
git pull origin main

echo "==> Vérification des variables d'environnement"
set -a
source infra/.env
set +a
bash infra/scripts/check_prod_env.sh

echo "==> Construction des images"
compose build

echo "==> Démarrage de PostgreSQL et Redis"
compose up -d --wait db redis

echo "==> Migrations (conteneur ponctuel)"
compose run --rm backend-django python manage.py migrate

echo "==> Rôle PostgreSQL en lecture seule de FastAPI"
COMPOSE_FILE="$COMPOSE_PROD" bash infra/scripts/create_fastapi_readonly_role.sh

echo "==> Démarrage de toute la pile"
compose up -d

echo "==> Nettoyage des anciennes images inutilisées"
docker image prune -f

echo "==> Statut des conteneurs"
compose ps

echo "✅ Déploiement terminé."
