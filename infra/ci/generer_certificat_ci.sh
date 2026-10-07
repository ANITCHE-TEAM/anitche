#!/usr/bin/env bash
# infra/ci/generer_certificat_ci.sh
# Certificat autosigné de CI pour anitche.com et www.anitche.com, à
# l'emplacement où nginx.conf le lit (live/anitche.com/{fullchain,privkey}.pem
# sous le dossier monté sur /etc/letsencrypt). Valable un jour.
#
# Usage : bash infra/ci/generer_certificat_ci.sh <dossier>

set -euo pipefail

DOSSIER="${1:?Usage : generer_certificat_ci.sh <dossier>}"
LIVE="$DOSSIER/live/anitche.com"

mkdir -p "$LIVE"
openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
    -keyout "$LIVE/privkey.pem" -out "$LIVE/fullchain.pem" \
    -subj "/CN=anitche.com" \
    -addext "subjectAltName=DNS:anitche.com,DNS:www.anitche.com" 2>/dev/null
# nginx est lancé en root dans le conteneur, mais le dossier est monté en
# lecture seule depuis un utilisateur sans privilèges : droits explicites.
chmod 755 "$DOSSIER" "$DOSSIER/live" "$LIVE"
chmod 644 "$LIVE/privkey.pem" "$LIVE/fullchain.pem"

echo "Certificat de CI écrit dans $LIVE."
