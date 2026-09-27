#!/usr/bin/env bash
# F-18 (audit sécurité) : émission INITIALE du certificat Let's Encrypt
# pour anitche.com / www.anitche.com.
#
# À lancer UNE SEULE FOIS, avant le tout premier démarrage de Nginx avec le
# bloc "listen 443 ssl" (infra/nginx/nginx.conf) : Nginx refuse de démarrer
# si le certificat qu'il référence n'existe pas encore, mais Certbot a
# besoin que Nginx tourne déjà (en HTTP) pour répondre au challenge ACME.
# On casse cette dépendance circulaire avec un certificat "factice"
# temporaire, le temps que Nginx démarre et que le vrai certificat soit
# obtenu — méthode standard, documentée par Certbot lui-même.
#
# Ensuite, le renouvellement est automatique via le service "certbot" de
# docker-compose.prod.yml : ce script ne sert plus qu'en cas de changement
# de domaine ou de perte totale du volume de certificats.
#
# Usage : ./infra/scripts/init-lets-encrypt.sh

set -euo pipefail

cd "$(dirname "$0")/../.."   # racine du repo

COMPOSE="docker compose -f infra/docker-compose.prod.yml --env-file infra/.env"
DOMAINES=(anitche.com www.anitche.com)
DOMAINE_PRINCIPAL="${DOMAINES[0]}"
RSA_KEY_SIZE=4096

if [ -z "${CERTBOT_EMAIL:-}" ]; then
    echo "❌ ERREUR : la variable CERTBOT_EMAIL n'est pas définie dans infra/.env."
    echo "   Nécessaire pour les alertes Let's Encrypt (expiration, révocation)."
    exit 1
fi

echo "==> Préparation des volumes de certificats"
$COMPOSE up -d nginx --no-deps --build 2>/dev/null || true
$COMPOSE stop nginx 2>/dev/null || true

echo "==> Création d'un certificat factice temporaire pour ${DOMAINE_PRINCIPAL}"
CHEMIN_LIVE="/etc/letsencrypt/live/${DOMAINE_PRINCIPAL}"
$COMPOSE run --rm --entrypoint "\
  sh -c 'mkdir -p ${CHEMIN_LIVE} && \
  openssl req -x509 -nodes -newkey rsa:1024 -days 1 \
    -keyout ${CHEMIN_LIVE}/privkey.pem \
    -out ${CHEMIN_LIVE}/fullchain.pem \
    -subj \"/CN=localhost\"'" certbot

echo "==> Démarrage de Nginx avec le certificat factice"
$COMPOSE up -d nginx

echo "==> Suppression du certificat factice"
$COMPOSE run --rm --entrypoint "\
  sh -c 'rm -rf /etc/letsencrypt/live/${DOMAINE_PRINCIPAL} && \
  rm -rf /etc/letsencrypt/archive/${DOMAINE_PRINCIPAL} && \
  rm -rf /etc/letsencrypt/renewal/${DOMAINE_PRINCIPAL}.conf'" certbot

echo "==> Demande du vrai certificat Let's Encrypt pour : ${DOMAINES[*]}"
DOMAIN_ARGS=""
for d in "${DOMAINES[@]}"; do
    DOMAIN_ARGS="$DOMAIN_ARGS -d $d"
done

$COMPOSE run --rm --entrypoint "\
  certbot certonly --webroot -w /var/www/certbot \
    $DOMAIN_ARGS \
    --email $CERTBOT_EMAIL \
    --rsa-key-size $RSA_KEY_SIZE \
    --agree-tos \
    --no-eff-email" certbot

echo "==> Rechargement de Nginx avec le vrai certificat"
$COMPOSE exec nginx nginx -s reload

echo "✅ Certificat Let's Encrypt émis pour ${DOMAINES[*]}. Le renouvellement est désormais automatique (service 'certbot')."
