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
#   5. rôle PostgreSQL applicatif anitche_app (infra/postgres/app_role.sql),
#      propriétaire de la base : les migrations et l'application s'en
#      servent, jamais du superutilisateur ;
#   6. migrations par infra/scripts/migrer.sh : s'il y en a en attente,
#      sauvegarde de la base d'abord (backup.sh), arrêt si elle échoue ;
#   7. rôle PostgreSQL en lecture seule de FastAPI, recréé à chaque
#      déploiement : ses droits portent sur des tables et des vues créées
#      par les migrations (infra/postgres/fastapi_readonly.sql) ;
#   8. démarrage de toute la pile ;
#   9. nginx relit son modèle (infra/nginx/nginx.conf) et se recharge sans
#      coupure : « up -d » ne recrée nginx que si son service a changé dans
#      le compose, pas quand seul le contenu du modèle a changé ;
#  10. statut.
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

echo "==> Rôle PostgreSQL applicatif"
COMPOSE_FILE="$COMPOSE_PROD" bash infra/scripts/create_app_role.sh

echo "==> Migrations (sauvegarde préalable s'il y en a en attente)"
ENV_FILE=infra/.env COMPOSE_FILE="$COMPOSE_PROD" bash infra/scripts/migrer.sh

echo "==> Rôle PostgreSQL en lecture seule de FastAPI"
COMPOSE_FILE="$COMPOSE_PROD" bash infra/scripts/create_fastapi_readonly_role.sh

echo "==> Démarrage de toute la pile"
compose up -d

echo "==> Rechargement de nginx (modèle rendu à nouveau, configuration testée)"
# Mêmes scripts qu'au démarrage du conteneur ; nginx -t refuse une
# configuration invalide avant tout rechargement (l'ancienne reste servie,
# et le déploiement s'arrête en erreur).
compose exec -T nginx sh -c '/docker-entrypoint.d/20-envsubst-on-templates.sh > /dev/null &&
    /docker-entrypoint.d/40-anitche-config.sh && nginx -t -q && nginx -s reload'

echo "==> Nettoyage des anciennes images inutilisées"
docker image prune -f

echo "==> Statut des conteneurs"
compose ps

echo "✅ Déploiement terminé."
