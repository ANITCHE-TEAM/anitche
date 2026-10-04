#!/usr/bin/env bash
# infra/scripts/backup.sh
# Sauvegarde chiffrée hors serveur de la base et des médias
# (docs/SAUVEGARDES.md). Lancé chaque nuit à 3 h UTC par cron, sur le
# serveur de production :
#   0 3 * * * /srv/anitche/infra/scripts/backup.sh > /dev/null 2>&1
#
# Étapes :
#   1. charge infra/.env et vérifie les variables de la sauvegarde (noms
#      seulement, jamais les valeurs) ;
#   2. construit l'image « backup » (cache de Docker : quelques secondes
#      quand infra/backup/ n'a pas changé) ;
#   3. lance le conteneur à usage unique « backup » (infra/backup/entrypoint.sh) :
#      base, médias, rétention, contrôle du dépôt ; le dimanche (UTC), ou
#      avec --controle-approfondi, contrôle approfondi qui relit une part
#      des données ;
#   4. journal horodaté (UTC) dans infra/backups/backup.log ;
#   5. après une réussite complète seulement, appelle BACKUP_PING_URL : le
#      service de surveillance alerte quand l'appel d'une nuit manque
#      (échec, cron arrêté, serveur éteint).
# Sort avec le code du conteneur (3 : fichier média illisible, compté comme
# un échec).
#
# Usage : infra/scripts/backup.sh [--controle-approfondi]

set -euo pipefail
umask 077

cd "$(dirname "$0")/../.."   # racine du dépôt

COMPOSE_PROD=infra/docker-compose.prod.yml
JOURNAL=infra/backups/backup.log
REQUISES=(
    DB_NAME DB_USER DB_PASSWORD
    BACKUP_REPOSITORY BACKUP_PASSWORD BACKUP_S3_ACCESS_KEY_ID BACKUP_S3_SECRET_ACCESS_KEY
)

compose() { docker compose -f "$COMPOSE_PROD" --env-file infra/.env --profile backup "$@"; }
journal() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$JOURNAL"; }
# Commande dont la sortie va aussi au journal ; rend son propre code.
executer() {
    "$@" 2>&1 | tee -a "$JOURNAL"
    return "${PIPESTATUS[0]}"
}

option=''
case "${1:-}" in
    '') ;;
    --controle-approfondi) option=--controle-approfondi ;;
    *) echo "Usage : $0 [--controle-approfondi]" >&2; exit 2 ;;
esac
if [ "$(date -u +%u)" = 7 ]; then
    option=--controle-approfondi
fi

mkdir -p infra/backups
chmod 700 infra/backups
debut=$(date -u +%s)
journal "sauvegarde : début${option:+ ($option)}"

if [ ! -f infra/.env ]; then
    journal "sauvegarde : ÉCHEC (infra/.env introuvable)"
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
    journal "sauvegarde : ÉCHEC (absentes ou vides dans infra/.env : ${manquantes[*]})"
    exit 1
fi

code=0
executer compose build --quiet backup || code=$?
if [ "$code" -eq 0 ]; then
    executer compose run --rm --no-deps -T backup sauvegarder ${option:+"$option"} || code=$?
else
    journal "image : ÉCHEC de la construction (code $code)"
fi

duree=$(( $(date -u +%s) - debut ))
if [ "$code" -ne 0 ]; then
    journal "sauvegarde : ÉCHEC (code $code, $duree s)"
    exit "$code"
fi
journal "sauvegarde : OK ($duree s)"

# Ni l'adresse (elle contient l'identifiant du contrôle) ni la réponse ne
# vont au journal. Un appel manqué ne change pas le code de sortie : le
# service de surveillance alertera faute d'appel.
if [ -n "${BACKUP_PING_URL:-}" ]; then
    if curl -fs -m 10 --retry 3 -o /dev/null "$BACKUP_PING_URL" 2> /dev/null; then
        journal "surveillance : appel envoyé"
    else
        journal "surveillance : AVERTISSEMENT, appel non envoyé (code curl $?)"
    fi
fi
exit 0
