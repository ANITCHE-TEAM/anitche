#!/usr/bin/env bash
# infra/ci/smoke_prod.sh
# Contrôles de la pile de production démarrée par le job (compose de
# production plus infra/ci/docker-compose.ci.yml), à travers nginx.
#
# Sous-commandes, à lancer depuis la racine du dépôt :
#   refus_demarrage  prod.py refuse un fournisseur simulé, un SMTP absent et un
#                    Redis sans mot de passe
#                    (conteneur ponctuel, avant le démarrage de la pile)
#   attendre         attend un 200 de Django à travers nginx (120 s au plus)
#   verifier         routage, statiques, médias, route interne, webhook,
#                    documentation fermée, routes de santé fermées,
#                    X-Request-ID, conteneurs, sécurité (Redis sans mot de
#                    passe refusé, rôle PostgreSQL non superutilisateur,
#                    conteneurs sans root)
#   securite         les seuls contrôles de sécurité de « verifier »
#   resolver         nginx redémarre sans backend-fastapi (502 sur /fast/),
#                    puis le sert sans rechargement dès que FastAPI revient
#                    (à lancer en dernier : recrée nginx et FastAPI)
#
# Variables : ENV_CI, CI_CERT_DIR, COMPOSE_PROJECT_NAME (voir compose.sh).
# Les requêtes visent https://anitche.com résolu vers 127.0.0.1, certificat
# vérifié (--cacert, jamais -k). Chaque attente et chaque requête a un délai.
# Une requête fautive est affichée en détail (statut, en-têtes et début du
# corps de la réponse) ; la requête elle-même n'est jamais affichée avec ses
# en-têtes, donc un jeton n'apparaît pas.

set -uo pipefail

: "${CI_CERT_DIR:?CI_CERT_DIR doit désigner le dossier du certificat de test}"

HOTE=anitche.com
URL="https://$HOTE"
CACERT="$CI_CERT_DIR/live/anitche.com/fullchain.pem"
TMP="$(mktemp -d)"
ECHECS=()
DESCRIPTION=""
COURANT_OK=1
STATUT=""
JETON_EXPIRE=""

compose() { bash infra/ci/compose.sh "$@"; }

# --- Requêtes -----------------------------------------------------------------

# appel "<libellé>" <méthode> <url> [options curl...]
appel() {
    local libelle="$1" methode="$2" url="$3"
    shift 3
    DESCRIPTION="$libelle : $methode $url"
    COURANT_OK=1
    : > "$TMP/corps"; : > "$TMP/entetes"; : > "$TMP/erreur"
    STATUT="$(curl -sS --max-time 15 --connect-timeout 5 -X "$methode" \
        -D "$TMP/entetes" -o "$TMP/corps" -w '%{http_code}' \
        --resolve "$HOTE:443:127.0.0.1" --resolve "$HOTE:80:127.0.0.1" \
        --cacert "$CACERT" "$@" "$url" 2>"$TMP/erreur")" || STATUT="000"
}

detail() {
    echo "   Requête : $DESCRIPTION" >&2
    echo "   Statut  : $STATUT" >&2
    if [ -s "$TMP/erreur" ]; then echo "   Erreur curl : $(head -c 300 "$TMP/erreur")" >&2; fi
    echo "   --- en-têtes de la réponse" >&2
    sed 's/^/   | /' "$TMP/entetes" >&2
    echo "   --- corps (600 premiers octets)" >&2
    head -c 600 "$TMP/corps" | sed 's/^/   | /' >&2
    echo >&2
}

ko() {
    COURANT_OK=0
    ECHECS+=("$DESCRIPTION : $1")
    echo "❌ $DESCRIPTION : $1" >&2
    detail
}

statut_attendu() { [ "$STATUT" = "$1" ] || ko "statut $STATUT, $1 attendu"; }
entete_attendu() { grep -qi -E "^$1:.*$2" "$TMP/entetes" || ko "en-tête $1 absent ou différent de /$2/"; }
corps_attendu() { grep -q -E "$1" "$TMP/corps" || ko "corps sans /$1/"; }
corps_json_valide() { python3 -c 'import json, sys; json.load(sys.stdin)' < "$TMP/corps" 2>/dev/null || ko "corps qui n'est pas du JSON valide"; }
conclure() { if [ "$COURANT_OK" = 1 ]; then echo "✅ $DESCRIPTION"; fi; return 0; }

# Une vérification qui ne passe pas par curl (conteneurs, commandes).
marquer() {
    DESCRIPTION="$1"; COURANT_OK=1; STATUT="-"
    : > "$TMP/corps"; : > "$TMP/entetes"; : > "$TMP/erreur"
}

# --- refus_demarrage ----------------------------------------------------------

# $1 : libellé, $2 : NOM=valeur imposé au conteneur ponctuel.
refuser_prod() {
    local libelle="$1" variable="$2" sortie code
    marquer "prod.py refuse $libelle"
    sortie="$(timeout 120 bash infra/ci/compose.sh run --rm --no-deps -T -e "$variable" backend-django \
        python -c "import config.settings.prod" 2>&1)"
    code=$?
    printf '%s\n' "$sortie" | tail -n 5 > "$TMP/corps"
    if [ "$code" -eq 0 ]; then
        STATUT="code 0"; ko "prod.py a démarré alors qu'il devait refuser"
    elif ! grep -q ImproperlyConfigured <<<"$sortie"; then
        STATUT="code $code"; ko "refus sans ImproperlyConfigured"
    fi
    conclure
}

refus_demarrage() {
    local sortie code
    marquer "prod.py accepte l'environnement de CI (témoin)"
    sortie="$(timeout 120 bash infra/ci/compose.sh run --rm --no-deps -T backend-django python -c "import config.settings.prod" 2>&1)"
    code=$?
    if [ "$code" -ne 0 ]; then
        printf '%s\n' "$sortie" | tail -n 5 > "$TMP/corps"
        STATUT="code $code"; ko "prod.py refuse l'environnement de CI"
    fi
    conclure
    refuser_prod "PAIEMENT_FOURNISSEUR=simule" "PAIEMENT_FOURNISSEUR=simule"
    refuser_prod "un EMAIL_HOST vide" "EMAIL_HOST="
    refuser_prod "un REDIS_URL sans mot de passe" "REDIS_URL=redis://redis:6379/0"
}

# --- attendre -----------------------------------------------------------------

attendre() {
    local limite=$((SECONDS + 120))
    while [ "$SECONDS" -lt "$limite" ]; do
        appel "Django répond à travers nginx" GET "$URL/api/catalogue/categories/"
        if [ "$STATUT" = 200 ]; then echo "✅ $DESCRIPTION"; return 0; fi
        sleep 3
    done
    echo "❌ Django ne répond pas 200 à travers nginx après 120 s." >&2
    detail
    return 1
}

# --- verifier -----------------------------------------------------------------

preparer_donnees() {
    # Jeton d'accès expiré, fabriqué dans le conteneur avec sa SECRET_KEY.
    JETON_EXPIRE="$(timeout 90 bash infra/ci/compose.sh exec -T backend-django python manage.py shell -c "
from datetime import timedelta
from rest_framework_simplejwt.tokens import AccessToken
jeton = AccessToken()
jeton.set_exp(lifetime=timedelta(seconds=-60))
print(jeton)
" 2>/dev/null | tail -n 1)"
    marquer "Fabrication d'un jeton expiré dans backend-django"
    if [ -z "$JETON_EXPIRE" ]; then
        ko "aucun jeton produit"
        JETON_EXPIRE="expire.invalide.jeton"
    else
        echo "::add-mask::$JETON_EXPIRE"
    fi
    conclure

    # Un fichier par dossier média : quatre publics (ceux de la location
    # /media/ de nginx.conf), trois privés.
    marquer "Écriture des fichiers de test dans le volume média"
    if ! timeout 60 bash infra/ci/compose.sh exec -T backend-django sh -c '
        mkdir -p /app/media/catalogue/produits /app/media/catalogue/categories /app/media/boutiques/logos /app/media/boutiques/bannieres /app/media/kyc /app/media/support/pieces_jointes /app/media/retours/preuves &&
        echo public > /app/media/catalogue/produits/ci.txt &&
        echo public > /app/media/catalogue/categories/ci.txt &&
        echo public > /app/media/boutiques/logos/ci.txt &&
        echo public > /app/media/boutiques/bannieres/ci.txt &&
        echo prive > /app/media/kyc/ci.txt &&
        echo prive > /app/media/support/pieces_jointes/ci.txt &&
        echo prive > /app/media/retours/preuves/ci.txt'; then
        ko "écriture impossible"
    fi
    conclure
}

verifier_routage() {
    appel "HTTP redirigé vers HTTPS" GET "http://$HOTE/api/catalogue/categories/"
    statut_attendu 301; entete_attendu location "https://$HOTE/api/catalogue/categories/"; conclure

    appel "/api/ garde son préfixe (Django répond en JSON)" GET "$URL/api/catalogue/categories/"
    statut_attendu 200; entete_attendu content-type "application/json"; corps_json_valide
    entete_attendu strict-transport-security "max-age="; conclure

    appel "Route publique, jeton expiré ignoré" GET "$URL/api/catalogue/categories/" \
        -H "Authorization: Bearer $JETON_EXPIRE"
    statut_attendu 200; conclure

    appel "Route publique, jeton invalide ignoré" GET "$URL/api/catalogue/categories/" \
        -H "Authorization: Bearer n.importe.quoi"
    statut_attendu 200; conclure

    appel "Témoin : route protégée sans jeton" GET "$URL/api/utilisateurs/profil/"
    statut_attendu 401; conclure

    appel "Témoin : route protégée, jeton expiré" GET "$URL/api/utilisateurs/profil/" \
        -H "Authorization: Bearer $JETON_EXPIRE"
    statut_attendu 401; conclure

    appel "Frontend (substitut) servi par nginx" GET "$URL/"
    statut_attendu 200; corps_attendu "ANITCHE CI"; conclure

    appel "Administration Django via nginx" GET "$URL/admin/login/"
    statut_attendu 200; corps_attendu "/static/"; conclure
}

verifier_statiques_medias() {
    local prive public

    appel "Fichier statique de collectstatic" GET "$URL/static/admin/css/base.css"
    statut_attendu 200; entete_attendu content-type "text/css"
    entete_attendu cache-control "max-age=86400"; entete_attendu x-content-type-options nosniff; conclure

    appel "Média public servi" GET "$URL/media/catalogue/produits/ci.txt"
    statut_attendu 200; corps_attendu "^public"
    entete_attendu cache-control "max-age=604800"; entete_attendu x-content-type-options nosniff; conclure

    for public in catalogue/categories boutiques/logos boutiques/bannieres; do
        appel "Média public servi ($public)" GET "$URL/media/$public/ci.txt"
        statut_attendu 200; corps_attendu "^public"
        entete_attendu cache-control "max-age=604800"; conclure
    done

    appel "Média public absent" GET "$URL/media/catalogue/produits/absent.txt"
    statut_attendu 404; conclure

    # 404 produit par nginx (location /media/) : son corps contient « nginx »,
    # celui de Django non, qui répondrait aussi 404 si le bloc disparaissait.
    for prive in kyc support/pieces_jointes retours/preuves; do
        appel "Média privé refusé ($prive)" GET "$URL/media/$prive/ci.txt"
        statut_attendu 404; corps_attendu nginx; conclure
    done
    appel "Média privé refusé (remontée par ..)" GET "$URL/media/catalogue/produits/../../kyc/ci.txt" --path-as-is
    statut_attendu 404; conclure
    appel "Média privé refusé (// dans le chemin)" GET "$URL/media//kyc/ci.txt" --path-as-is
    statut_attendu 404; conclure
}

verifier_route_interne() {
    local chemin methode code

    # 404 produit par nginx : son corps contient « nginx », celui de Django non.
    for chemin in /api/utilisateurs/jeton/verification/ /api/utilisateurs/jeton/verification \
                  //api/utilisateurs/jeton/verification/ /api/utilisateurs/jeton/./verification/ \
                  /api/utilisateurs/jeton/%76erification/; do
        for methode in GET POST; do
            appel "Route interne fermée" "$methode" "$URL$chemin" --path-as-is
            statut_attendu 404; corps_attendu nginx; conclure
        done
    done

    marquer "Témoin : FastAPI joint la route interne de Django sur le réseau Docker"
    code="$(timeout 60 bash infra/ci/compose.sh exec -T backend-fastapi python -c "
import urllib.request, urllib.error
try:
    print(urllib.request.urlopen('http://backend-django:8000/api/utilisateurs/jeton/verification/', timeout=5).status)
except urllib.error.HTTPError as erreur:
    print(erreur.code)
except Exception as erreur:
    print(type(erreur).__name__)
" 2>/dev/null | tail -n 1)"
    STATUT="$code"
    [ "$code" = 401 ] || ko "code $code, 401 attendu (ni 404, ni 301 vers https, ni 400)"
    conclure
}

verifier_fastapi() {
    # /fast/health est interne (404 à l'extérieur) : le préfixe /fast retiré
    # est prouvé par la recherche, dont la route est /recherche/produits.
    appel "FastAPI : recherche publique (préfixe /fast retiré)" GET "$URL/fast/recherche/produits"
    statut_attendu 200; corps_attendu '"count": *0'; conclure

    appel "FastAPI : recherche publique, jeton invalide ignoré" GET "$URL/fast/recherche/produits" \
        -H "Authorization: Bearer n.importe.quoi"
    statut_attendu 200; conclure

    appel "FastAPI : suggestions" GET "$URL/fast/recherche/suggestions?recherche=abc"
    statut_attendu 200; conclure
}

verifier_webhooks_et_documentation() {
    local chemin

    # Sans signature valide, les routes de notification (paiement et
    # transfert) doivent refuser, à travers nginx.
    for chemin in /api/paiements/webhook/geniuspay/ /api/paiements/webhook/geniuspay/transfert/; do
        appel "Webhook sans signature refusé" POST "$URL$chemin" \
            -H "Content-Type: application/json" --data '{}'
        statut_attendu 401; corps_attendu "non authentique"; conclure
    done

    for chemin in /api/docs/ /api/schema/ /api/redoc/ /fast/docs /fast/redoc /fast/openapi.json; do
        appel "Documentation fermée" GET "$URL$chemin"
        statut_attendu 404; conclure
    done
}

verifier_sante_et_tracabilite() {
    local chemin rid

    # Les routes de santé sont internes : 404 produit par nginx (son corps
    # contient « nginx »), jamais le frontend ni Django.
    for chemin in /health /health/ /fast/health /fast/health/ /fast/healthz; do
        appel "Route de santé fermée à l'extérieur" GET "$URL$chemin" --path-as-is
        statut_attendu 404; corps_attendu nginx; conclure
    done

    # Le healthcheck de nginx écoute sur 127.0.0.1:8081 du conteneur nginx :
    # un autre conteneur du réseau n'y accède pas (connexion refusée).
    marquer "/nginx-health injoignable depuis les autres conteneurs"
    STATUT="$(timeout 60 bash infra/ci/compose.sh exec -T backend-django python -c "
import urllib.request, urllib.error
try:
    print(urllib.request.urlopen('http://nginx:8081/nginx-health', timeout=5).status)
except urllib.error.HTTPError as erreur:
    print(erreur.code)
except OSError:
    print('refuse')
" 2>&1 | tail -n 1)"
    [ "$STATUT" = refuse ] || ko "réponse « $STATUT », connexion refusée attendue"
    conclure

    # Un identifiant de 32 caractères hexadécimaux par réponse, y compris sur
    # un 404 produit par nginx.
    for chemin in /api/catalogue/categories/ /fast/recherche/produits /static/admin/css/base.css \
                  /media/kyc/ci.txt; do
        appel "X-Request-ID renvoyé ($chemin)" GET "$URL$chemin"
        entete_attendu x-request-id '[0-9a-f]{32}'; conclure
    done

    # Celui qu'envoie le client est écrasé.
    appel "X-Request-ID du client ignoré" GET "$URL/api/catalogue/categories/" -H "X-Request-ID: forge-par-le-client"
    entete_attendu x-request-id '[0-9a-f]{32}'
    ! grep -qi "forge-par-le-client" "$TMP/entetes" || ko "l'identifiant envoyé par le client est renvoyé"
    conclure

    # Le même identifiant est dans le journal d'accès de nginx et dans celui
    # de Gunicorn (rid=).
    appel "X-Request-ID relevé pour les journaux" GET "$URL/api/catalogue/categories/"
    rid="$(grep -i '^x-request-id:' "$TMP/entetes" | tr -d '\r' | awk '{print $2}')"
    marquer "X-Request-ID ${rid:-absent} écrit dans les journaux de nginx et de Gunicorn"
    sleep 2
    [ -n "$rid" ] || ko "aucun identifiant relevé"
    compose logs --no-color nginx 2>/dev/null | grep -q "rid=$rid" || ko "absent du journal de nginx"
    compose logs --no-color backend-django 2>/dev/null | grep -q "rid=$rid" || ko "absent du journal de Gunicorn"
    conclure
}

verifier_conteneurs() {
    local service id etat

    for service in backend-django celery-worker celery-beat backend-fastapi nginx; do
        marquer "Conteneur $service en marche, sans redémarrage"
        id="$(compose ps -q "$service" 2>/dev/null)"
        etat="$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' "$id" 2>/dev/null)"
        STATUT="$etat"
        [ "$etat" = "true 0" ] || ko "état « $etat », « true 0 » attendu (en marche, 0 redémarrage)"
        conclure
    done

    # Chaque service qui a un healthcheck doit être « healthy » (frontend et
    # certbot n'en ont pas).
    for service in db redis backend-django celery-worker celery-beat backend-fastapi nginx; do
        marquer "Healthcheck de $service"
        id="$(compose ps -q "$service" 2>/dev/null)"
        etat="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}sans-healthcheck{{end}}' "$id" 2>/dev/null)"
        STATUT="$etat"
        [ "$etat" = "healthy" ] || ko "santé « $etat », « healthy » attendu"
        conclure
    done

    marquer "/health/ de Django répond 200 depuis son conteneur (base et Redis)"
    etat="$(timeout 60 bash infra/ci/compose.sh exec -T backend-django python -c "
import urllib.request
print(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8000/health/', headers={'Host': 'backend-django'}), timeout=5).read().decode())
" 2>&1 | tail -n 1)"
    STATUT="$etat"
    [ "$etat" = '{"status": "ok"}' ] || ko "réponse « $etat », {\"status\": \"ok\"} attendu"
    conclure

    marquer "/health de FastAPI répond 200 depuis son conteneur (base et Redis)"
    etat="$(timeout 60 bash infra/ci/compose.sh exec -T backend-fastapi python -c "
import urllib.request
print(urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=5).status)
" 2>&1 | tail -n 1)"
    STATUT="$etat"
    [ "$etat" = 200 ] || ko "code « $etat », 200 attendu"
    conclure

    # Rotation des journaux appliquée par Docker à chaque conteneur créé.
    for id in $(compose ps -q 2>/dev/null); do
        service="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$id" 2>/dev/null)"
        marquer "Rotation des journaux de $service"
        etat="$(docker inspect -f '{{.HostConfig.LogConfig.Type}} {{index .HostConfig.LogConfig.Config "max-size"}} {{index .HostConfig.LogConfig.Config "max-file"}}' "$id" 2>/dev/null)"
        STATUT="$etat"
        [ "$etat" = "json-file 10m 5" ] || ko "journaux « $etat », « json-file 10m 5 » attendu"
        conclure
    done

    marquer "Worker Celery joint le broker (ping)"
    if ! timeout 90 bash infra/ci/compose.sh exec -T celery-worker celery -A config inspect ping --timeout 10 > "$TMP/corps" 2>&1 \
            || ! grep -q pong "$TMP/corps"; then
        ko "pas de pong"
    fi
    conclure
}

verifier_securite() {
    local service sortie

    # Sans mot de passe, Redis refuse toute commande. redis-cli tourne dans le
    # conteneur redis et passe par le réseau (-h redis), REDISCLI_AUTH retiré.
    marquer "Redis refuse une connexion sans mot de passe (NOAUTH)"
    sortie="$(timeout 30 bash infra/ci/compose.sh exec -T redis env -u REDISCLI_AUTH redis-cli -h redis ping 2>&1)"
    STATUT="$sortie"
    grep -q NOAUTH <<<"$sortie" || ko "réponse « $sortie », NOAUTH attendu"
    conclure

    marquer "Témoin : Redis répond PONG avec le mot de passe"
    sortie="$(timeout 30 bash infra/ci/compose.sh exec -T redis redis-cli -h redis ping 2>&1)"
    STATUT="$sortie"
    [ "$sortie" = PONG ] || ko "réponse « $sortie », PONG attendu"
    conclure

    # Django et Celery se connectent avec le rôle applicatif, jamais avec un
    # superutilisateur.
    marquer "Django : rôle PostgreSQL anitche_app, non superutilisateur"
    sortie="$(timeout 90 bash infra/ci/compose.sh exec -T backend-django python manage.py shell -c "
from django.db import connection
with connection.cursor() as curseur:
    curseur.execute('SELECT current_user, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = current_user')
    print(*curseur.fetchone())
" 2>&1 | tail -n 1)"
    STATUT="$sortie"
    [ "$sortie" = "anitche_app False False False" ] \
        || ko "réponse « $sortie », « anitche_app False False False » attendu (current_user, rolsuper, rolcreatedb, rolcreaterole)"
    conclure

    for service in backend-django celery-worker celery-beat backend-fastapi; do
        marquer "Conteneur $service sans root (id -u ≠ 0)"
        sortie="$(timeout 30 bash infra/ci/compose.sh exec -T "$service" id -u 2>&1 | tail -n 1)"
        STATUT="$sortie"
        [[ "$sortie" =~ ^[0-9]+$ && "$sortie" != 0 ]] || ko "id -u « $sortie », un uid non nul attendu"
        conclure
    done
}

verifier() {
    preparer_donnees
    verifier_routage
    verifier_statiques_medias
    verifier_route_interne
    verifier_fastapi
    verifier_webhooks_et_documentation
    verifier_sante_et_tracabilite
    verifier_conteneurs
    verifier_securite

    if [ "${#ECHECS[@]}" -gt 0 ]; then
        echo >&2
        echo "❌ ${#ECHECS[@]} contrôle(s) en échec :" >&2
        printf '   - %s\n' "${ECHECS[@]}" >&2
        return 1
    fi
    echo "✅ Tous les contrôles de la pile de production sont passés."
}

# --- resolver -----------------------------------------------------------------

# attendre_statut <url> <statut> : attend 60 s au plus ce statut à travers nginx.
attendre_statut() {
    local limite=$((SECONDS + 60))
    while [ "$SECONDS" -lt "$limite" ]; do
        appel "$3" GET "$1"
        if [ "$STATUT" = "$2" ]; then return 0; fi
        sleep 2
    done
    return 1
}

resolver() {
    local id_nginx demarre_avant demarre_apres

    # FastAPI absent, nginx recréé : sans resolver, nginx refuse de démarrer
    # (« host not found in upstream »).
    marquer "nginx démarre sans FastAPI"
    compose rm -sf backend-fastapi >/dev/null 2>&1 || ko "FastAPI non retiré"
    if ! compose up -d --no-deps --force-recreate --wait --wait-timeout 60 nginx >/dev/null 2>"$TMP/erreur"; then
        ko "nginx ne démarre pas sans FastAPI"
        compose logs --no-color --tail=20 nginx >&2 || true
        conclure; return
    fi
    conclure

    id_nginx="$(compose ps -q nginx)"
    demarre_avant="$(docker inspect -f '{{.State.StartedAt}}' "$id_nginx")"

    attendre_statut "$URL/fast/recherche/produits" 502 "FastAPI absent : 502 produit par nginx" || true
    statut_attendu 502; conclure

    appel "Django reste servi pendant l'absence de FastAPI" GET "$URL/api/catalogue/categories/"
    statut_attendu 200; conclure

    marquer "FastAPI relancé et sain"
    compose up -d --no-deps --wait --wait-timeout 120 backend-fastapi >/dev/null 2>"$TMP/erreur" || ko "FastAPI ne redevient pas sain"
    conclure

    # Sans reload ni redémarrage de nginx : la résolution suit le conteneur.
    if attendre_statut "$URL/fast/recherche/produits" 200 "nginx sert FastAPI relancé, sans rechargement"; then
        corps_attendu '"count": *0'
    else
        ko "statut $STATUT, 200 attendu dans les 60 s"
    fi
    conclure

    marquer "nginx n'a ni redémarré ni été rechargé"
    demarre_apres="$(docker inspect -f '{{.State.StartedAt}}' "$id_nginx")"
    [ "$demarre_avant" = "$demarre_apres" ] || ko "nginx a redémarré ($demarre_avant puis $demarre_apres)"
    if compose logs --no-color nginx 2>/dev/null | grep -q "signal process started"; then
        ko "nginx a reçu un signal de rechargement"
    fi
    conclure
}

case "${1:-}" in
    refus_demarrage) refus_demarrage ;;
    attendre) attendre; exit $? ;;
    verifier) verifier; exit $? ;;
    securite) verifier_securite ;;
    resolver) resolver ;;
    *) echo "Usage : smoke_prod.sh refus_demarrage|attendre|verifier|securite|resolver" >&2; exit 2 ;;
esac

if [ "${#ECHECS[@]}" -gt 0 ]; then exit 1; fi
