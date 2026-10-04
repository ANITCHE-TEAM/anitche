#!/usr/bin/env bash
# infra/scripts/restore.sh
# Restauration d'une sauvegarde (docs/SAUVEGARDES.md), dans le conteneur à
# usage unique « backup » (infra/backup/entrypoint.sh). N'écrase jamais de
# données : la base cible doit être absente ou vide, le dossier des médias
# vide ; sinon refus.
#
#   restore.sh --lister
#       instantanés de la base et des médias, avec leur date ;
#   restore.sh [--base NOM] [--instantane-base ID] [--instantane-medias ID]
#       exercice : base jetable (anitche_restauration_AAAAMMJJ par défaut),
#       médias dans un dossier temporaire du conteneur ;
#   restore.sh --reprise [--instantane-base ID] [--instantane-medias ID]
#       reprise après sinistre : base DB_NAME et volume anitche_media, tous
#       deux vides.
# Puis vérifications : extensions, nombres de lignes, fichiers référencés
# par la base présents dans les médias restaurés, durée ; sortie non nulle
# si l'une échoue. Instantanés les plus récents par défaut, sinon ceux
# affichés par --lister (base et médias de la même nuit).
#
# Le service « db » doit tourner (docker compose … up -d --wait db).

set -euo pipefail

cd "$(dirname "$0")/../.."   # racine du dépôt

COMPOSE_PROD=infra/docker-compose.prod.yml
REQUISES=(
    DB_NAME DB_USER DB_PASSWORD
    BACKUP_REPOSITORY BACKUP_PASSWORD BACKUP_S3_ACCESS_KEY_ID BACKUP_S3_SECRET_ACCESS_KEY
)

compose() { docker compose -f "$COMPOSE_PROD" --env-file infra/.env --profile backup "$@"; }
usage() {
    echo "Usage : $0 --lister" >&2
    echo "        $0 [--base NOM] [--instantane-base ID] [--instantane-medias ID]" >&2
    echo "        $0 --reprise [--instantane-base ID] [--instantane-medias ID]" >&2
    exit 2
}

mode=exercice
base=()
instantanes=()
while [ $# -gt 0 ]; do
    case "$1" in
        --lister) mode=lister; shift ;;
        --reprise) mode=reprise; shift ;;
        --base)
            [ $# -ge 2 ] || usage
            base=(--base "$2"); shift 2 ;;
        --instantane-base|--instantane-medias)
            [ $# -ge 2 ] || usage
            instantanes+=("$1" "$2"); shift 2 ;;
        *) usage ;;
    esac
done
# La reprise restaure toujours dans DB_NAME.
if [ "$mode" = reprise ] && [ "${#base[@]}" -gt 0 ]; then
    usage
fi

if [ ! -f infra/.env ]; then
    echo "❌ infra/.env introuvable." >&2
    exit 1
fi
set -a
. infra/.env
set +a
manquantes=()
for var in "${REQUISES[@]}"; do
    [ -n "${!var:-}" ] || manquantes+=("$var")
done
if [ "${#manquantes[@]}" -gt 0 ]; then
    echo "❌ Absentes ou vides dans infra/.env : ${manquantes[*]}" >&2
    exit 1
fi

compose build --quiet backup
case "$mode" in
    lister)
        compose run --rm --no-deps -T backup instantanes ;;
    exercice)
        compose run --rm --no-deps -T backup restaurer "${base[@]}" "${instantanes[@]}" ;;
    reprise)
        # anitche_media en écriture pour cette exécution seulement (le service
        # le monte en lecture seule) ; Compose le résout en volume du projet.
        compose run --rm --no-deps -T -v anitche_media:/restauration/medias backup \
            restaurer --base "$DB_NAME" --medias /restauration/medias "${instantanes[@]}" ;;
esac
