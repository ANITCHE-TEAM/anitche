#!/usr/bin/env bash
# infra/ci/verifier_compose.sh
# Contrôles d'hygiène du compose de production, sans démarrer la pile.
#
# Sous-commandes, à lancer depuis la racine du dépôt :
#   statique   aucun avertissement « obsolete » ; environnement identique
#              pour backend-django, celery-worker et celery-beat ; rotation
#              des journaux 10m x 5 sur chaque service ; scripts de
#              infra/scripts/ en mode 100755 dans Git
#   gunicorn   réglages de backend-django/gunicorn.conf.py, lus par
#              « gunicorn --print-config » dans l'image construite
#
# Variables : ENV_CI, CI_CERT_DIR, COMPOSE_PROJECT_NAME (voir compose.sh).

set -uo pipefail

ECHECS=0

ko() { echo "❌ $1" >&2; ECHECS=$((ECHECS + 1)); }
ok() { echo "✅ $1"; }

compose() { bash infra/ci/compose.sh "$@"; }

controle_obsolete() {
    local erreurs
    erreurs="$(mktemp)"
    compose config -q 2> "$erreurs" || ko "docker compose config échoue : $(head -c 600 "$erreurs")"
    if grep -qi obsolete "$erreurs"; then
        ko "avertissement « obsolete » (prod + CI) : $(grep -i obsolete "$erreurs" | head -n 3)"
    else
        ok "prod + CI : aucun avertissement « obsolete »"
    fi
    # Le compose de développement n'a pas besoin de l'environnement de CI.
    if ! docker compose -f infra/docker-compose.yml config -q 2> "$erreurs"; then
        ko "docker compose config (développement) échoue : $(head -c 600 "$erreurs")"
    elif grep -qi obsolete "$erreurs"; then
        ko "avertissement « obsolete » (développement) : $(grep -i obsolete "$erreurs" | head -n 3)"
    else
        ok "développement : aucun avertissement « obsolete »"
    fi
    rm -f -- "$erreurs"
}

# Le profil « backup » est inclus pour que le service backup soit contrôlé.
controle_config_json() {
    local json resultat
    json="$(mktemp)"
    if ! compose --profile backup config --format json > "$json"; then
        ko "docker compose config --format json échoue"
        rm -f -- "$json"
        return
    fi
    resultat="$(python3 - "$json" <<'PY'
import json, sys

services = json.load(open(sys.argv[1]))["services"]
erreurs = []

refs = [s for s in ("backend-django", "celery-worker", "celery-beat") if s in services]
if len(refs) != 3:
    erreurs.append("services Django/worker/beat introuvables : %s" % refs)
else:
    env = {s: services[s].get("environment") for s in refs}
    if not env["backend-django"]:
        erreurs.append("environnement de backend-django vide")
    for s in refs[1:]:
        if env[s] != env["backend-django"]:
            ecart = sorted(
                k for k in set(env[s]) | set(env["backend-django"])
                if env[s].get(k) != env["backend-django"].get(k)
            )
            erreurs.append("environnement de %s différent de backend-django : %s" % (s, ecart))

for nom, service in sorted(services.items()):
    log = service.get("logging") or {}
    opts = log.get("options") or {}
    if log.get("driver") != "json-file" or opts.get("max-size") != "10m" or str(opts.get("max-file")) != "5":
        erreurs.append("%s : journaux %s au lieu de json-file 10m x 5" % (nom, log))

print("\n".join(erreurs))
print("services:%d" % len(services), file=sys.stderr)
PY
)"
    if [ -n "$resultat" ]; then
        while IFS= read -r ligne; do ko "$ligne"; done <<<"$resultat"
    else
        ok "environnement identique pour backend-django, celery-worker et celery-beat"
        ok "rotation des journaux 10m x 5 sur chaque service"
    fi
    rm -f -- "$json"
}

controle_modes() {
    local mode fichier faux=0
    while read -r mode _ _ fichier; do
        if [ "$mode" != 100755 ]; then ko "$fichier : mode $mode dans Git, 100755 attendu"; faux=1; fi
    done < <(git ls-files -s 'infra/scripts/*.sh')
    if [ "$faux" = 0 ]; then ok "infra/scripts/*.sh : tous en 100755"; fi
}

statique() {
    controle_obsolete
    controle_config_json
    controle_modes
}

# $1 : variable GUNICORN_WORKERS ("" = non définie), $2 : workers attendus.
config_gunicorn() {
    local sortie
    if [ -n "$1" ]; then
        sortie="$(timeout 120 bash infra/ci/compose.sh run --rm --no-deps -T -e "GUNICORN_WORKERS=$1" backend-django \
            gunicorn --print-config -c gunicorn.conf.py config.wsgi:application 2>&1)"
    else
        sortie="$(timeout 120 bash infra/ci/compose.sh run --rm --no-deps -T backend-django \
            gunicorn --print-config -c gunicorn.conf.py config.wsgi:application 2>&1)"
    fi
    # --print-config aligne les valeurs (« workers         = 3 ») : on ramène
    # chaque ligne à « nom = valeur » avant de comparer.
    local normalisee
    normalisee="$(sed -E 's/^[[:space:]]+//; s/[[:space:]]*=[[:space:]]*/ = /; s/[[:space:]]+$//' <<<"$sortie")"
    local attendu absent=0
    for attendu in "workers = $2" "timeout = 30" "graceful_timeout = 30" "max_requests = 1000" \
                   "max_requests_jitter = 100" "worker_tmp_dir = /dev/shm" "accesslog = -"; do
        if ! grep -qxF "$attendu" <<<"$normalisee"; then
            ko "gunicorn --print-config (GUNICORN_WORKERS='${1}') : « $attendu » absent"
            absent=1
        fi
    done
    if [ "$absent" = 1 ]; then
        echo "--- début de la sortie de gunicorn --print-config (GUNICORN_WORKERS='${1}') ---"
        head -n 40 <<<"$sortie"
        echo "--- fin ---"
    fi
}

reglages_gunicorn() {
    local avant="$ECHECS"
    config_gunicorn "" 3
    config_gunicorn 5 5
    if [ "$ECHECS" = "$avant" ]; then ok "gunicorn.conf.py : réglages attendus (3 workers par défaut, GUNICORN_WORKERS pris en compte)"; fi
}

case "${1:-}" in
    statique) statique ;;
    gunicorn) reglages_gunicorn ;;
    *) echo "Usage : verifier_compose.sh statique|gunicorn" >&2; exit 2 ;;
esac

if [ "$ECHECS" -gt 0 ]; then exit 1; fi
