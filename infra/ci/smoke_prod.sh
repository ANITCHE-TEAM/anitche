#!/usr/bin/env bash
# infra/ci/smoke_prod.sh
# Contrôles de la pile de production démarrée par le job (compose de
# production plus infra/ci/docker-compose.ci.yml), à travers nginx.
#
# Sous-commandes, à lancer depuis la racine du dépôt :
#   refus_demarrage  prod.py refuse un fournisseur simulé et un SMTP absent
#                    (conteneur ponctuel, avant le démarrage de la pile)
#   attendre         attend un 200 de Django à travers nginx (120 s au plus)
#   verifier         routage, statiques, médias, route interne, webhook,
#                    documentation fermée, conteneurs
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

    # Un fichier par dossier média : un public (catalogue/produits), trois privés.
    marquer "Écriture des fichiers de test dans le volume média"
    if ! timeout 60 bash infra/ci/compose.sh exec -T backend-django sh -c '
        mkdir -p /app/media/catalogue/produits /app/media/kyc /app/media/support/pieces_jointes /app/media/retours/preuves &&
        echo public > /app/media/catalogue/produits/ci.txt &&
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
    local prive

    appel "Fichier statique de collectstatic" GET "$URL/static/admin/css/base.css"
    statut_attendu 200; entete_attendu content-type "text/css"
    entete_attendu cache-control "max-age=86400"; entete_attendu x-content-type-options nosniff; conclure

    appel "Média public servi" GET "$URL/media/catalogue/produits/ci.txt"
    statut_attendu 200; corps_attendu "^public"
    entete_attendu cache-control "max-age=604800"; entete_attendu x-content-type-options nosniff; conclure

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
    appel "FastAPI : état du service (préfixe /fast retiré)" GET "$URL/fast/health"
    statut_attendu 200; corps_attendu '"status": *"ok"'; conclure

    appel "FastAPI : recherche publique" GET "$URL/fast/recherche/produits"
    statut_attendu 200; corps_attendu '"count": *0'; conclure

    appel "FastAPI : recherche publique, jeton invalide ignoré" GET "$URL/fast/recherche/produits" \
        -H "Authorization: Bearer n.importe.quoi"
    statut_attendu 200; conclure

    appel "FastAPI : suggestions" GET "$URL/fast/recherche/suggestions?recherche=abc"
    statut_attendu 200; conclure
}

verifier_webhooks_et_documentation() {
    local chemin

    for chemin in /api/paiements/webhook/cinetpay/ /api/paiements/webhook/cinetpay/transfert/; do
        appel "Webhook sans signature refusé" POST "$URL$chemin" \
            -H "Content-Type: application/json" --data '{}'
        statut_attendu 401; corps_attendu "non authentique"; conclure
    done

    for chemin in /api/docs/ /api/schema/ /api/redoc/ /fast/docs /fast/redoc /fast/openapi.json; do
        appel "Documentation fermée" GET "$URL$chemin"
        statut_attendu 404; conclure
    done
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

    marquer "Worker Celery joint le broker (ping)"
    if ! timeout 90 bash infra/ci/compose.sh exec -T celery-worker celery -A config inspect ping --timeout 10 > "$TMP/corps" 2>&1 \
            || ! grep -q pong "$TMP/corps"; then
        ko "pas de pong"
    fi
    conclure
}

verifier() {
    preparer_donnees
    verifier_routage
    verifier_statiques_medias
    verifier_route_interne
    verifier_fastapi
    verifier_webhooks_et_documentation
    verifier_conteneurs

    if [ "${#ECHECS[@]}" -gt 0 ]; then
        echo >&2
        echo "❌ ${#ECHECS[@]} contrôle(s) en échec :" >&2
        printf '   - %s\n' "${ECHECS[@]}" >&2
        return 1
    fi
    echo "✅ Tous les contrôles de la pile de production sont passés."
}

case "${1:-}" in
    refus_demarrage) refus_demarrage ;;
    attendre) attendre; exit $? ;;
    verifier) verifier; exit $? ;;
    *) echo "Usage : smoke_prod.sh refus_demarrage|attendre|verifier" >&2; exit 2 ;;
esac

if [ "${#ECHECS[@]}" -gt 0 ]; then exit 1; fi
