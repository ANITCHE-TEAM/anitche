#!/bin/sh
# infra/nginx/entrypoint.d/50-anitche-rechargement.sh
# Lancé par l'entrypoint de l'image nginx officielle à chaque démarrage du
# conteneur, avant nginx. Démarre en arrière-plan une boucle qui recharge
# nginx toutes les NGINX_RELOAD_INTERVAL (6h par défaut ; nombre suivi de
# s, m, h ou d, comme sleep).
#
# Le service certbot renouvelle le certificat dans le volume partagé, mais
# nginx garde en mémoire celui lu au démarrage : sans rechargement, le
# certificat servi expirerait malgré le renouvellement. Le rechargement
# relit aussi la configuration rendue (pas le modèle) ; une configuration
# invalide est refusée par nginx, qui continue avec l'ancienne.

set -eu

ME=$(basename "$0")
INTERVALLE="${NGINX_RELOAD_INTERVAL:-6h}"

if ! printf '%s' "$INTERVALLE" | grep -Eq '^[1-9][0-9]*[smhd]?$'; then
    echo "$ME: ERREUR : NGINX_RELOAD_INTERVAL : nombre entier positif suivi de s, m, h ou d attendu (ex. 6h)." >&2
    exit 1
fi

# Entrées et sorties détachées de l'entrypoint ; la boucle s'arrête avec le
# conteneur.
(
    while :; do
        sleep "$INTERVALLE"
        echo "$ME: rechargement périodique de nginx (certificat et configuration relus)"
        nginx -s reload || echo "$ME: rechargement refusé par nginx, configuration précédente conservée" >&2
    done
) < /dev/null &

echo "$ME: rechargement de nginx toutes les $INTERVALLE."
