#!/bin/sh
# infra/nginx/entrypoint.d/40-anitche-config.sh
# Lancé par l'entrypoint de l'image nginx officielle à chaque démarrage du
# conteneur, après le rendu du modèle infra/nginx/nginx.conf
# (20-envsubst-on-templates.sh) et avant nginx. Un code de sortie non nul
# empêche nginx de démarrer : une valeur douteuse ferme le service plutôt
# que de l'ouvrir.
#
#   1. DOMAIN, DOMAIN_VENDEUR, DOMAIN_LIVREUR, DOMAIN_ADMIN : noms d'hôte en
#      minuscules (ils sont recopiés tels quels dans la configuration) ;
#   2. ADMIN_ALLOWED_CIDRS (adresses IPv4 ou IPv6, avec ou sans /préfixe,
#      séparées par des virgules ou des espaces) : une ligne « allow » par
#      entrée dans /etc/nginx/anitche/admin_autorises.conf, inclus par la
#      location /admin/ avant « deny all ». Vide : aucune ligne, /admin/
#      fermé à tous. Un préfixe /0 (tout Internet) est refusé.
#
# Mêmes règles que infra/scripts/check_prod_env.sh, qui les applique avant
# le déploiement ; nginx contrôle en plus la valeur numérique des adresses.

set -eu
# Pas d'expansion de « * » ou « ? » lors du découpage de ADMIN_ALLOWED_CIDRS.
set -f

ME=$(basename "$0")
FICHIER_ADMIN=/etc/nginx/anitche/admin_autorises.conf
MOTIF_HOTE='^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'
MOTIF_IPV4='^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$'
MOTIF_IPV6='^[0-9a-fA-F:.]*:[0-9a-fA-F:.]*(/[0-9]{1,3})?$'

refus() {
    echo "$ME: ERREUR : $*" >&2
    exit 1
}

for nom in DOMAIN DOMAIN_VENDEUR DOMAIN_LIVREUR DOMAIN_ADMIN; do
    eval "valeur=\${$nom:-}"
    [ -n "$valeur" ] || refus "$nom absente ou vide."
    printf '%s' "$valeur" | grep -Eq "$MOTIF_HOTE" \
        || refus "$nom : nom d'hôte en minuscules attendu (ex. anitche.com), sans schéma, port ni « / »."
done

mkdir -p "$(dirname "$FICHIER_ADMIN")"
{
    echo "# Écrit par $ME au démarrage, depuis ADMIN_ALLOWED_CIDRS."
    for entree in $(printf '%s' "${ADMIN_ALLOWED_CIDRS:-}" | tr ',' ' '); do
        if ! printf '%s' "$entree" | grep -Eq "$MOTIF_IPV4" \
                && ! printf '%s' "$entree" | grep -Eq "$MOTIF_IPV6"; then
            refus "ADMIN_ALLOWED_CIDRS : « $entree » n'est pas une adresse IPv4 ou IPv6 (avec ou sans /préfixe)."
        fi
        case "$entree" in
            */0) refus "ADMIN_ALLOWED_CIDRS : « $entree » ouvrirait /admin/ à tout Internet." ;;
        esac
        echo "allow $entree;"
    done
} > "$FICHIER_ADMIN.tmp"
mv "$FICHIER_ADMIN.tmp" "$FICHIER_ADMIN"

autorisees=$(grep -c '^allow ' "$FICHIER_ADMIN" || true)
if [ "$autorisees" = 0 ]; then
    echo "$ME: /admin/ fermé à toutes les adresses (ADMIN_ALLOWED_CIDRS vide)."
else
    echo "$ME: /admin/ ouvert à $autorisees entrée(s) de ADMIN_ALLOWED_CIDRS."
fi
