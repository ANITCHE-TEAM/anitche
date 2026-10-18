#!/usr/bin/env bash
# infra/ci/generer_certificat_ci.sh
# Certificat autosigné de CI pour DOMAIN (staging.exemple.test par défaut),
# www.DOMAIN et les sous-domaines des portails, à l'emplacement où
# nginx.conf le lit (live/DOMAIN/{fullchain,privkey}.pem sous le dossier
# monté sur /etc/letsencrypt). Valable un jour. Relancé sur le même dossier,
# il remplace le certificat (nouveau numéro de série) : c'est ainsi que la CI
# simule un renouvellement par certbot.
#
# Usage : bash infra/ci/generer_certificat_ci.sh <dossier>

set -euo pipefail

DOSSIER="${1:?Usage : generer_certificat_ci.sh <dossier>}"
DOMAIN="${DOMAIN:-staging.exemple.test}"
LIVE="$DOSSIER/live/$DOMAIN"

mkdir -p "$LIVE"
# Fichiers temporaires puis renommage : nginx ne lit jamais un fichier à
# moitié écrit (un rechargement entre les deux renommages est refusé par
# nginx, qui garde l'ancien certificat jusqu'au suivant).
openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
    -keyout "$LIVE/privkey.pem.tmp" -out "$LIVE/fullchain.pem.tmp" \
    -subj "/CN=$DOMAIN" \
    -addext "subjectAltName=DNS:$DOMAIN,DNS:www.$DOMAIN,DNS:vendeur.$DOMAIN,DNS:livreur.$DOMAIN,DNS:admin.$DOMAIN" 2>/dev/null
mv -f "$LIVE/privkey.pem.tmp" "$LIVE/privkey.pem"
mv -f "$LIVE/fullchain.pem.tmp" "$LIVE/fullchain.pem"
# nginx est lancé en root dans le conteneur, mais le dossier est monté en
# lecture seule depuis un utilisateur sans privilèges : droits explicites.
chmod 755 "$DOSSIER" "$DOSSIER/live" "$LIVE"
chmod 644 "$LIVE/privkey.pem" "$LIVE/fullchain.pem"

echo "Certificat de CI écrit dans $LIVE."
