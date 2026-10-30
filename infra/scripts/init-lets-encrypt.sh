#!/usr/bin/env bash
# Émission INITIALE du certificat Let's Encrypt, un seul certificat (SAN)
# pour DOMAIN, www.DOMAIN et les sous-domaines des portails
# (DOMAIN_VENDEUR, DOMAIN_LIVREUR, DOMAIN_ADMIN ; par défaut vendeur.,
# livreur. et admin. de DOMAIN), lus dans infra/.env. Validation par webroot
# (challenge HTTP sur le port 80) : chacun de ces noms doit déjà pointer
# vers ce serveur dans le DNS (docs/DEPLOIEMENT.md).
#
# À lancer UNE SEULE FOIS, avant le tout premier démarrage de Nginx avec le
# bloc "listen 443 ssl" (infra/nginx/nginx.conf) : Nginx refuse de démarrer
# si le certificat qu'il référence n'existe pas encore, mais Certbot a
# besoin que Nginx tourne déjà (en HTTP) pour répondre au challenge ACME.
# On casse cette dépendance circulaire avec un certificat "factice"
# temporaire, le temps que Nginx démarre et que le vrai certificat soit
# obtenu — méthode standard, documentée par Certbot lui-même.
#
# Le certificat porte le nom DOMAIN (--cert-name) : nginx le lit dans
# /etc/letsencrypt/live/DOMAIN/, quel que soit l'ordre des noms.
#
# Ensuite, le renouvellement est automatique via le service "certbot" de
# docker-compose.prod.yml, et nginx sert le certificat renouvelé à son
# rechargement périodique (NGINX_RELOAD_INTERVAL) : ce script n'est utile
# qu'en cas de changement de domaine, d'ajout d'un nom ou de perte totale du
# volume de certificats.
#
# Usage : ./infra/scripts/init-lets-encrypt.sh
# Variable : ENV_FILE (fichier d'environnement, infra/.env par défaut).

set -euo pipefail

cd "$(dirname "$0")/../.."   # racine du repo

ENV_FILE="${ENV_FILE:-infra/.env}"
if [ ! -f "$ENV_FILE" ]; then
    echo "❌ ERREUR : $ENV_FILE introuvable."
    exit 1
fi
set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

compose() { docker compose -f infra/docker-compose.prod.yml --env-file "$ENV_FILE" "$@"; }
RSA_KEY_SIZE=4096

if [ -z "${CERTBOT_EMAIL:-}" ]; then
    echo "❌ ERREUR : la variable CERTBOT_EMAIL n'est pas définie dans $ENV_FILE."
    echo "   Nécessaire pour les alertes Let's Encrypt (expiration, révocation)."
    exit 1
fi
if [ -z "${DOMAIN:-}" ]; then
    echo "❌ ERREUR : la variable DOMAIN n'est pas définie dans $ENV_FILE (ex. anitche.com)."
    exit 1
fi
# Mêmes valeurs par défaut que le service nginx de docker-compose.prod.yml.
DOMAINES=(
    "$DOMAIN"
    "www.$DOMAIN"
    "${DOMAIN_VENDEUR:-vendeur.$DOMAIN}"
    "${DOMAIN_LIVREUR:-livreur.$DOMAIN}"
    "${DOMAIN_ADMIN:-admin.$DOMAIN}"
)

echo "==> Préparation des volumes de certificats"
compose up -d nginx --no-deps 2>/dev/null || true
compose stop nginx 2>/dev/null || true

echo "==> Création d'un certificat factice temporaire pour ${DOMAIN}"
CHEMIN_LIVE="/etc/letsencrypt/live/${DOMAIN}"
compose run --rm --entrypoint "\
  sh -c 'mkdir -p ${CHEMIN_LIVE} && \
  openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
    -keyout ${CHEMIN_LIVE}/privkey.pem \
    -out ${CHEMIN_LIVE}/fullchain.pem \
    -subj \"/CN=localhost\"'" certbot

echo "==> Démarrage de Nginx avec le certificat factice"
compose up -d nginx --no-deps

echo "==> Suppression du certificat factice"
compose run --rm --entrypoint "\
  sh -c 'rm -rf /etc/letsencrypt/live/${DOMAIN} && \
  rm -rf /etc/letsencrypt/archive/${DOMAIN} && \
  rm -rf /etc/letsencrypt/renewal/${DOMAIN}.conf'" certbot

echo "==> Demande du vrai certificat Let's Encrypt pour : ${DOMAINES[*]}"
DOMAIN_ARGS=""
for d in "${DOMAINES[@]}"; do
    DOMAIN_ARGS="$DOMAIN_ARGS -d $d"
done

compose run --rm --entrypoint "\
  certbot certonly --webroot -w /var/www/certbot \
    --cert-name $DOMAIN \
    $DOMAIN_ARGS \
    --email $CERTBOT_EMAIL \
    --rsa-key-size $RSA_KEY_SIZE \
    --agree-tos \
    --no-eff-email" certbot

echo "==> Rechargement de Nginx avec le vrai certificat"
compose exec nginx nginx -s reload

echo "✅ Certificat Let's Encrypt émis pour ${DOMAINES[*]}. Renouvellement automatique (service 'certbot'), servi par nginx à son rechargement périodique."
