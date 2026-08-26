#!/usr/bin/env bash
# Script de déploiement simple pour un VPS (Hetzner, DigitalOcean, etc.)
# Suppose que le repo est déjà cloné sur le serveur et qu'un fichier
# infra/.env (non commité) contient les secrets de prod.
#
# Usage : ./infra/scripts/deploy.sh

set -euo pipefail

cd "$(dirname "$0")/../.."   # se placer à la racine du repo

echo "==> Récupération de la dernière version du code"
git pull origin main

echo "==> Build et redémarrage des conteneurs"
docker compose -f infra/docker-compose.prod.yml --env-file infra/.env up -d --build

echo "==> Nettoyage des anciennes images inutilisées"
docker image prune -f

echo "==> Statut des conteneurs"
docker compose -f infra/docker-compose.prod.yml ps

echo "✅ Déploiement terminé."
