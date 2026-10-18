#!/usr/bin/env bash
# infra/ci/smoke_prod.sh
# Contrôles de la pile de production démarrée par le job (compose de
# production plus infra/ci/docker-compose.ci.yml), à travers nginx.
#
# Sous-commandes, à lancer depuis la racine du dépôt :
#   refus_demarrage  prod.py refuse un fournisseur simulé, un SMTP absent et un
#                    Redis sans mot de passe
#                    (conteneur ponctuel, avant le démarrage de la pile)
#   migrations       migrer.sh : une sauvegarde en échec bloque tout,
#                    sinon sauvegarde (instantané db dans le dépôt restic
#                    local de CI) puis migrations ; rien en attente : ni
#                    l'une ni l'autre (avant le démarrage de la pile)
#   attendre         attend un 200 de Django à travers nginx (120 s au plus)
#   portails         les quatre portails, chacun sur son nom d'hôte : page,
#                    URL profonde, cache des assets et d'index.html, CSP, API
#                    et médias sur la même origine (inclus dans « verifier »)
#   verifier         routage, portails, /admin/ fermé, Host et SNI inconnus
#                    refusés, configuration nginx rendue pour DOMAIN, statiques,
#                    médias, route interne, webhook, documentation fermée,
#                    routes de santé fermées, X-Request-ID, conteneurs,
#                    sécurité (Redis sans mot de passe refusé, rôle
#                    PostgreSQL non superutilisateur, conteneurs sans root)
#   securite         les contrôles de sécurité de la pile démarrée : /admin/,
#                    Host et SNI inconnus, Redis, rôle PostgreSQL, root
#   resolver         nginx redémarre sans backend-fastapi (502 sur /fast/),
#                    puis le sert sans rechargement dès que FastAPI revient ;
#                    Django recréé avec une autre adresse IP, servi sans
#                    rechargement (recrée nginx, FastAPI et Django)
#   certificat       nginx recréé avec NGINX_RELOAD_INTERVAL=10s, nouveau
#                    certificat de CI posé : servi en moins de 20 s, sans
#                    intervention (à lancer en dernier)
#
# Variables : DOMAIN, ENV_CI, CI_CERT_DIR, COMPOSE_PROJECT_NAME (voir
# compose.sh). Les requêtes visent https://$DOMAIN résolu vers 127.0.0.1,
# certificat vérifié (--cacert, jamais -k). Chaque attente et chaque
# requête a un délai.
# Une requête fautive est affichée en détail (statut, en-têtes et début du
# corps de la réponse) ; la requête elle-même n'est jamais affichée avec ses
# en-têtes, donc un jeton n'apparaît pas.

set -uo pipefail

: "${CI_CERT_DIR:?CI_CERT_DIR doit désigner le dossier du certificat de test}"

HOTE="${DOMAIN:?DOMAIN doit désigner le domaine de la pile de CI}"
URL="https://$HOTE"
CACERT="$CI_CERT_DIR/live/$HOTE/fullchain.pem"
TMP="$(mktemp -d)"
ECHECS=()
DESCRIPTION=""
COURANT_OK=1
STATUT=""
CODE_CURL=0
# Tous les noms servis par nginx, résolus vers la pile locale.
RESOLUTIONS=()
for nom in "$HOTE" "www.$HOTE" "vendeur.$HOTE" "livreur.$HOTE" "admin.$HOTE"; do
    RESOLUTIONS+=(--resolve "$nom:443:127.0.0.1" --resolve "$nom:80:127.0.0.1")
done
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
    CODE_CURL=0
    STATUT="$(curl -sS --max-time 15 --connect-timeout 5 -X "$methode" \
        -D "$TMP/entetes" -o "$TMP/corps" -w '%{http_code}' \
        "${RESOLUTIONS[@]}" --cacert "$CACERT" "$@" "$url" 2>"$TMP/erreur")" \
        || { CODE_CURL=$?; STATUT="000"; }
}

detail() {
    echo "   Requête : $DESCRIPTION" >&2
    echo "   Statut  : $STATUT (code curl $CODE_CURL)" >&2
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
# Connexion refusée par nginx, avec le code de curl qui la distingue d'une
# autre erreur : 52 (fermée sans réponse, return 444 ; 56 quand cette
# fermeture interrompt une session TLS, selon la bibliothèque TLS de curl),
# 35 (poignée de main TLS refusée). Un certificat qui ne correspond pas
# donnerait 60. $@ : codes acceptés.
connexion_refusee() {
    local code
    if [ "$STATUT" = 000 ]; then
        for code in "$@"; do [ "$CODE_CURL" = "$code" ] && return 0; done
    fi
    ko "statut $STATUT, code curl $CODE_CURL ; connexion refusée attendue (code curl $*)"
}
corps_json_valide() { python3 -c 'import json, sys; json.load(sys.stdin)' < "$TMP/corps" 2>/dev/null || ko "corps qui n'est pas du JSON valide"; }
conclure() { if [ "$COURANT_OK" = 1 ]; then echo "✅ $DESCRIPTION"; fi; return 0; }

# Une vérification qui ne passe pas par curl (conteneurs, commandes).
marquer() {
    DESCRIPTION="$1"; COURANT_OK=1; STATUT="-"; CODE_CURL=0
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

}

# Un portail par nom d'hôte : client sur le domaine principal, les autres sur
# leur sous-domaine (vendeur., livreur. et admin. de DOMAIN dans la CI).
verifier_portails() {
    local portail hote url racine asset prive
    for portail in client vendeur livreur admin; do
        if [ "$portail" = client ]; then hote="$HOTE"; else hote="$portail.$HOTE"; fi
        url="https://$hote"
        racine="$TMP/racine-$portail.html"

        appel "Portail $portail : page d'accueil" GET "$url/"
        statut_attendu 200; entete_attendu x-portail "$portail"; corps_attendu 'id="root"'
        entete_attendu content-type "text/html"; entete_attendu cache-control "no-cache"
        cp "$TMP/corps" "$racine"; conclure

        # CSP présente, sans eval ni origine extérieure.
        appel "Portail $portail : Content-Security-Policy" GET "$url/"
        entete_attendu content-security-policy "default-src 'self'"
        entete_attendu content-security-policy "frame-ancestors 'none'"
        entete_attendu content-security-policy "style-src-elem 'self' 'unsafe-inline'"
        entete_attendu content-security-policy "object-src 'none'"
        if grep -i '^content-security-policy:' "$TMP/entetes" | grep -qE "unsafe-eval|https?:|[*]"; then
            ko "CSP trop large (unsafe-eval, origine extérieure ou joker)"
        fi
        conclure

        # Repli de l'application : une URL profonde reçoit index.html.
        appel "Portail $portail : URL profonde servie par index.html" GET "$url/commandes/12/details"
        statut_attendu 200; entete_attendu x-portail "$portail"; entete_attendu cache-control "no-cache"
        cmp -s "$TMP/corps" "$racine" || ko "le corps diffère de la page d'accueil"
        conclure

        asset="$(grep -oE '/assets/[^"]+[.]js' "$racine" | head -n 1)"
        marquer "Portail $portail : fichier du bundle repéré dans index.html"
        [ -n "$asset" ] || ko "aucun /assets/*.js dans index.html"
        conclure
        appel "Portail $portail : asset immuable" GET "$url${asset:-/assets/absent.js}"
        statut_attendu 200; entete_attendu cache-control "max-age=31536000"
        entete_attendu cache-control "immutable"; entete_attendu content-type "javascript"; conclure

        # Un fichier du bundle disparu est un 404, jamais index.html (le
        # navigateur l'exécuterait comme du JavaScript), et non mis en cache.
        appel "Portail $portail : asset absent" GET "$url/assets/absent-0123.js"
        statut_attendu 404
        if grep -qi '^cache-control:.*immutable' "$TMP/entetes"; then ko "404 marqué immuable"; fi
        conclure

        # Même origine : API Django, FastAPI et médias, sans CORS.
        appel "Portail $portail : API Django sur le sous-domaine" GET "$url/api/catalogue/categories/"
        statut_attendu 200; entete_attendu content-type "application/json"; corps_json_valide; conclure
        appel "Portail $portail : FastAPI sur le sous-domaine" GET "$url/fast/recherche/produits"
        statut_attendu 200; corps_attendu '"count": *0'; conclure
        appel "Portail $portail : média public servi" GET "$url/media/catalogue/produits/ci.txt"
        statut_attendu 200; corps_attendu "^public"; conclure
        # 404 de nginx (son corps contient « nginx ») : sans le bloc /media/,
        # le repli du portail répondrait 200 avec index.html.
        for prive in kyc support/pieces_jointes retours/preuves; do
            appel "Portail $portail : média privé refusé ($prive)" GET "$url/media/$prive/ci.txt"
            statut_attendu 404; corps_attendu nginx; conclure
        done
        appel "Portail $portail : route de santé fermée" GET "$url/health/"
        statut_attendu 404; corps_attendu nginx; conclure
        appel "Portail $portail : route interne de vérification du jeton fermée" GET "$url/api/utilisateurs/jeton/verification/"
        statut_attendu 404; corps_attendu nginx; conclure

        # /admin/ de Django n'existe que sur le domaine principal (403 testé
        # par verifier_admin) : ailleurs, c'est une URL du portail.
        if [ "$portail" != client ]; then
            appel "Portail $portail : /admin/login/ n'atteint pas Django" GET "$url/admin/login/"
            statut_attendu 200; entete_attendu x-portail "$portail"
            if grep -q csrfmiddlewaretoken "$TMP/corps"; then ko "page de connexion de Django servie"; fi
            conclure
        fi
    done
}

verifier_admin() {
    local chemin entete sortie

    # 403 produit par nginx (son corps contient « nginx »), sans atteindre
    # Django : l'hôte n'est pas dans ADMIN_ALLOWED_CIDRS (127.0.0.1/32 et
    # ::1/128 en CI ; vu de nginx, l'hôte arrive par la passerelle Docker).
    for chemin in /admin/login/ /admin/ //admin/login/ /%61dmin/login/ /api/../admin/login/ /static/../admin/login/; do
        appel "/admin/ fermé depuis l'hôte ($chemin)" GET "$URL$chemin" --path-as-is
        statut_attendu 403; corps_attendu nginx; conclure
    done
    appel "/admin/ fermé depuis l'hôte (POST de connexion)" POST "$URL/admin/login/" --data 'username=a&password=b'
    statut_attendu 403; corps_attendu nginx; conclure

    # Adresse forgée par le client : real_ip ne lit CF-Connecting-IP que
    # depuis une plage Cloudflare ; les autres en-têtes ne comptent jamais.
    for entete in "CF-Connecting-IP: 127.0.0.1" "X-Forwarded-For: 127.0.0.1" "X-Real-IP: 127.0.0.1"; do
        appel "/admin/ fermé malgré l'en-tête ${entete%%:*} forgé" GET "$URL/admin/login/" -H "$entete"
        statut_attendu 403; corps_attendu nginx; conclure
    done

    # Témoin : depuis le conteneur nginx, la connexion vient de 127.0.0.1,
    # autorisée en CI ; Django sert la page de connexion de l'administration.
    marquer "Témoin : /admin/login/ servi depuis le conteneur nginx (127.0.0.1 autorisé)"
    sortie="$(timeout 60 bash infra/ci/compose.sh exec -T nginx curl -sS --max-time 15 \
        --resolve "$HOTE:443:127.0.0.1" --cacert "/etc/letsencrypt/live/$HOTE/fullchain.pem" \
        -w '\nSTATUT=%{http_code}' "https://$HOTE/admin/login/" 2>&1)"
    STATUT="$(sed -n 's/^STATUT=//p' <<<"$sortie" | tail -n 1)"
    printf '%s\n' "$sortie" > "$TMP/corps"
    [ "$STATUT" = 200 ] || ko "statut « $STATUT », 200 attendu"
    corps_attendu "/static/admin/"
    conclure
}

verifier_hotes_inconnus() {
    local sous_domaine

    appel "HTTP, Host inconnu : connexion fermée sans réponse" GET "http://$HOTE/" -H "Host: inconnu.exemple.test"
    connexion_refusee 52; conclure
    appel "HTTP par l'adresse IP : connexion fermée sans réponse" GET "http://127.0.0.1/api/catalogue/categories/"
    connexion_refusee 52; conclure
    appel "HTTPS, SNI inconnu : poignée de main refusée" GET "https://inconnu.exemple.test/" \
        --resolve "inconnu.exemple.test:443:127.0.0.1"
    connexion_refusee 35; conclure
    appel "HTTPS par l'adresse IP (sans SNI) : poignée de main refusée" GET "https://127.0.0.1/"
    connexion_refusee 35; conclure
    # SNI et certificat valides, mais en-tête Host inconnu : bloc par défaut.
    appel "HTTPS, SNI connu et Host inconnu : connexion fermée sans réponse" GET "$URL/api/catalogue/categories/" \
        --http1.1 -H "Host: inconnu.exemple.test"
    connexion_refusee 52 56; conclure

    appel "Témoin : www.$HOTE servi en HTTPS" GET "https://www.$HOTE/api/catalogue/categories/"
    statut_attendu 200; conclure
    # Le challenge ACME de chaque nom du certificat atteint le webroot de
    # certbot (404 de nginx : fichier absent), au lieu d'une connexion fermée.
    for sous_domaine in vendeur livreur admin; do
        appel "Challenge ACME joignable pour $sous_domaine.$HOTE" GET "http://$sous_domaine.$HOTE/.well-known/acme-challenge/absent"
        statut_attendu 404; corps_attendu nginx; conclure
        appel "$sous_domaine.$HOTE redirigé vers HTTPS" GET "http://$sous_domaine.$HOTE/"
        statut_attendu 301; entete_attendu location "https://$sous_domaine.$HOTE/"; conclure
    done
}

# Configuration rendue par l'image nginx depuis le modèle nginx.conf.
verifier_rendu_nginx() {
    local portail
    marquer "Configuration nginx rendue pour DOMAIN=$HOTE"
    timeout 30 bash infra/ci/compose.sh exec -T nginx cat /etc/nginx/conf.d/default.conf > "$TMP/rendu" 2>&1 \
        || ko "configuration rendue illisible"
    grep -qxF "    server_name $HOTE www.$HOTE;" "$TMP/rendu" || ko "server_name du bloc 443 absent"
    grep -qxF "    server_name $HOTE www.$HOTE vendeur.$HOTE livreur.$HOTE admin.$HOTE;" "$TMP/rendu" \
        || ko "server_name du bloc 80 (tous les noms du certificat) absent"
    grep -qF "ssl_certificate     /etc/letsencrypt/live/$HOTE/fullchain.pem;" "$TMP/rendu" || ko "chemin du certificat absent"
    if grep -nE '\$\{[A-Za-z_]+\}' "$TMP/rendu" > "$TMP/corps"; then ko "variable non remplacée"; fi
    if [ "$HOTE" != anitche.com ] && grep -n 'anitche\.com' "$TMP/rendu" > "$TMP/corps"; then ko "anitche.com écrit en dur"; fi
    # Les variables de nginx ne sont pas des variables d'environnement.
    grep -qF 'rid=$request_id' "$TMP/rendu" || ko "\$request_id remplacé au rendu"
    for portail in vendeur livreur admin; do
        grep -qxF "    server_name $portail.$HOTE;" "$TMP/rendu" || ko "bloc 443 de $portail.$HOTE absent"
    done
    conclure

    # Les fichiers inclus sont lus tels quels : aucun nom de domaine, et les
    # variables de nginx intactes.
    marquer "Fichiers inclus par nginx : sans nom de domaine, variables de nginx intactes"
    timeout 30 bash infra/ci/compose.sh exec -T nginx cat /etc/nginx/anitche/serveur-commun.conf /etc/nginx/anitche/proxy-en-tetes.conf > "$TMP/rendu" 2>&1 \
        || ko "fichiers inclus illisibles"
    if [ "$HOTE" != anitche.com ] && grep -n 'anitche\.com' "$TMP/rendu" > "$TMP/corps"; then ko "anitche.com écrit en dur"; fi
    grep -qF 'proxy_set_header Host $host;' "$TMP/rendu" || ko "proxy_set_header Host absent"
    grep -qF 'add_header X-Request-ID $request_id always;' "$TMP/rendu" || ko "X-Request-ID absent"
    conclure

    marquer "Adresses autorisées sur /admin/ : exactement celles de ADMIN_ALLOWED_CIDRS"
    timeout 30 bash infra/ci/compose.sh exec -T nginx grep '^allow ' /etc/nginx/anitche/admin_autorises.conf > "$TMP/corps" 2>&1
    STATUT="$(tr '\n' ' ' < "$TMP/corps")"
    [ "$STATUT" = "allow 127.0.0.1/32; allow ::1/128; " ] || ko "« allow 127.0.0.1/32; allow ::1/128; » attendu"
    conclure
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

    for service in backend-django celery-worker celery-beat backend-fastapi nginx \
                   frontend-client frontend-vendeur frontend-livreur frontend-admin; do
        marquer "Conteneur $service en marche, sans redémarrage"
        id="$(compose ps -q "$service" 2>/dev/null)"
        etat="$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' "$id" 2>/dev/null)"
        STATUT="$etat"
        [ "$etat" = "true 0" ] || ko "état « $etat », « true 0 » attendu (en marche, 0 redémarrage)"
        conclure
    done

    # Chaque service qui a un healthcheck doit être « healthy » (certbot n'en
    # a pas).
    for service in db redis backend-django celery-worker celery-beat backend-fastapi nginx \
                   frontend-client frontend-vendeur frontend-livreur frontend-admin; do
        marquer "Healthcheck de $service"
        id="$(compose ps -q "$service" 2>/dev/null)"
        etat="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}sans-healthcheck{{end}}' "$id" 2>/dev/null)"
        STATUT="$etat"
        [ "$etat" = "healthy" ] || ko "santé « $etat », « healthy » attendu"
        conclure
    done

    # Les migrations ne passent que par migrer.sh (sauvegarde avant).
    marquer "backend-django : commande de démarrage sans migrate"
    etat="$(docker inspect -f '{{json .Config.Cmd}}' "$(compose ps -q backend-django 2>/dev/null)" 2>/dev/null)"
    STATUT="$etat"
    if grep -q migrate <<<"$etat"; then ko "la commande lance migrate"; fi
    grep -q gunicorn <<<"$etat" || ko "commande sans gunicorn"
    conclure

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

    for service in backend-django celery-worker celery-beat backend-fastapi \
                   frontend-client frontend-vendeur frontend-livreur frontend-admin; do
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
    verifier_portails
    verifier_admin
    verifier_hotes_inconnus
    verifier_rendu_nginx
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

adresse_ip() {
    docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$(compose ps -q "$1" 2>/dev/null)" 2>/dev/null
}

resolver() {
    local id_nginx demarre_avant demarre_apres ip_avant ip_apres reseau n occupants

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

    # Django recréé avec une autre adresse IP. Sans resolver, nginx
    # garderait l'ancienne (502 ou délai dépassé). Docker attribue la plus
    # basse adresse libre, souvent celle qui vient d'être libérée : des
    # conteneurs témoins occupent les adresses libres jusqu'à celle de
    # Django (10 au plus) le temps qu'il redémarre.
    marquer "Django recréé avec une autre adresse IP"
    ip_avant="$(adresse_ip backend-django)"
    reseau="$(docker inspect -f '{{range $nom, $r := .NetworkSettings.Networks}}{{$nom}}{{end}}' "$(compose ps -q backend-django)")"
    compose rm -sf backend-django >/dev/null 2>&1 || ko "Django non retiré"
    occupants=()
    for n in 1 2 3 4 5 6 7 8 9 10; do
        occupants+=("$COMPOSE_PROJECT_NAME-occupant-ip-$n")
        docker run -d --rm --name "$COMPOSE_PROJECT_NAME-occupant-ip-$n" --network "$reseau" \
            --entrypoint sleep nginx:alpine 300 >/dev/null 2>&1 || { ko "conteneur témoin non démarré"; break; }
        [ "$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' \
            "$COMPOSE_PROJECT_NAME-occupant-ip-$n" 2>/dev/null)" = "$ip_avant" ] && break
    done
    compose up -d --no-deps --wait --wait-timeout 120 backend-django >/dev/null 2>"$TMP/erreur" || ko "Django ne redevient pas sain"
    docker rm -f "${occupants[@]}" >/dev/null 2>&1 || true
    ip_apres="$(adresse_ip backend-django)"
    STATUT="${ip_avant:-?} puis ${ip_apres:-?}"
    if [ -z "$ip_avant" ] || [ -z "$ip_apres" ] || [ "$ip_avant" = "$ip_apres" ]; then
        ko "deux adresses différentes attendues (sinon le contrôle ne prouve rien)"
    fi
    conclure

    if attendre_statut "$URL/api/catalogue/categories/" 200 "nginx sert Django recréé, sans rechargement"; then
        corps_json_valide
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

# --- migrations ---------------------------------------------------------------

# Compose de production plus la surcharge de CI (dépôt restic local), pour
# migrer.sh et backup.sh qui lisent COMPOSE_FILE et ENV_FILE.
export COMPOSE_PATH_SEPARATOR=:
CI_COMPOSE_FILE=infra/docker-compose.prod.yml:infra/ci/docker-compose.ci.yml

restic_ci() {
    timeout 120 bash infra/ci/compose.sh --profile backup run --rm --no-deps -T --entrypoint restic backup "$@"
}

# Nombre d'instantanés de la base dans le dépôt de CI ; vide si illisible.
instantanes_db() {
    restic_ci snapshots --host anitche --tag db --json 2>/dev/null \
        | python3 -c 'import json, sys; print(len(json.load(sys.stdin) or []))' 2>/dev/null
}

migrer() {
    COMPOSE_FILE="$CI_COMPOSE_FILE" ENV_FILE="$1" timeout 900 bash infra/scripts/migrer.sh > "$TMP/corps" 2>&1
}

migrations_en_attente() {
    ! timeout 120 bash infra/ci/compose.sh run --rm --no-deps -T backend-django \
        python manage.py migrate --check --noinput > /dev/null 2>&1
}

migrations() {
    local avant apres code env_ko

    marquer "Dépôt restic local de CI prêt"
    if ! restic_ci cat config > /dev/null 2>&1; then
        timeout 120 bash infra/ci/compose.sh --profile backup run --rm --no-deps -T backup init > "$TMP/corps" 2>&1 \
            || ko "restic init échoue"
    fi
    avant="$(instantanes_db)"
    STATUT="${avant:-illisible} instantané(s) db"
    [[ "$avant" =~ ^[0-9]+$ ]] || ko "nombre d'instantanés illisible"
    conclure

    # Sauvegarde impossible (mot de passe du dépôt faux) : migrer.sh
    # s'arrête et la base reste sans migration.
    marquer "Sauvegarde en échec : migrer.sh s'arrête sans migrer"
    env_ko="$TMP/env_sauvegarde_ko"
    ( umask 077; sed 's/^BACKUP_PASSWORD=.*/BACKUP_PASSWORD=mot-de-passe-faux-de-ci-0123456789abcdef/' "$ENV_CI" > "$env_ko" )
    migrer "$env_ko"; code=$?
    rm -f -- "$env_ko"
    STATUT="code $code"
    [ "$code" -ne 0 ] || ko "migrer.sh a réussi malgré la sauvegarde en échec"
    grep -q "migrations NON appliquées" "$TMP/corps" || ko "message d'arrêt absent"
    migrations_en_attente || ko "des migrations ont été appliquées malgré la sauvegarde en échec"
    conclure

    marquer "migrer.sh : sauvegarde de la base, puis migrations"
    migrer "$ENV_CI" || ko "migrer.sh échoue"
    apres="$(instantanes_db)"
    STATUT="instantanés db : ${avant:-?} puis ${apres:-?}"
    [ "$apres" = "$((avant + 1))" ] || ko "un instantané db de plus attendu"
    ! migrations_en_attente || ko "migrations encore en attente"
    conclure

    marquer "migrer.sh sans migration en attente : ni sauvegarde ni migration"
    avant="$apres"
    migrer "$ENV_CI" || ko "migrer.sh échoue"
    grep -q "Aucune migration en attente" "$TMP/corps" || ko "message « Aucune migration en attente » absent"
    apres="$(instantanes_db)"
    STATUT="instantanés db : ${avant:-?} puis ${apres:-?}"
    [ "$apres" = "$avant" ] || ko "aucun nouvel instantané attendu"
    conclure
}

# --- certificat ---------------------------------------------------------------

# Numéro de série du certificat servi pour DOMAIN, et de celui du dossier
# de CI.
serie_servie() {
    timeout 10 openssl s_client -connect 127.0.0.1:443 -servername "$HOTE" < /dev/null 2>/dev/null \
        | openssl x509 -noout -serial 2>/dev/null
}
serie_fichier() { openssl x509 -noout -serial -in "$CACERT" 2>/dev/null; }

certificat() {
    local avant nouvelle servie debut limite

    marquer "nginx recréé avec NGINX_RELOAD_INTERVAL=10s"
    NGINX_RELOAD_INTERVAL=10s compose up -d --no-deps --force-recreate --wait --wait-timeout 60 nginx \
        > /dev/null 2>"$TMP/erreur" || ko "nginx ne démarre pas"
    avant="$(serie_servie)"
    STATUT="servi : ${avant:-aucun}"
    [ -n "$avant" ] && [ "$avant" = "$(serie_fichier)" ] || ko "le certificat servi n'est pas celui de CI"
    conclure

    # Renouvellement simulé : nouveau certificat dans le dossier monté, sans
    # toucher au conteneur nginx.
    bash infra/ci/generer_certificat_ci.sh "$CI_CERT_DIR" > /dev/null
    nouvelle="$(serie_fichier)"
    marquer "Certificat renouvelé servi sans intervention, en moins de 20 s"
    debut=$SECONDS
    limite=$((SECONDS + 20))
    servie=""
    while [ "$SECONDS" -lt "$limite" ]; do
        servie="$(serie_servie)"
        [ "$servie" = "$nouvelle" ] && break
        sleep 1
    done
    STATUT="servi : ${servie:-aucun} ; nouveau : $nouvelle ; ancien : $avant ; $((SECONDS - debut)) s"
    [ "$nouvelle" != "$avant" ] || ko "le nouveau certificat a le même numéro de série"
    [ "$servie" = "$nouvelle" ] || ko "nouveau certificat non servi après 20 s"
    conclure

    appel "Requête vérifiée avec le nouveau certificat" GET "$URL/api/catalogue/categories/"
    statut_attendu 200; conclure

    marquer "Rechargement périodique écrit dans le journal de nginx"
    compose logs --no-color nginx 2>/dev/null | grep -q "rechargement périodique de nginx" || ko "aucun rechargement journalisé"
    conclure
}

case "${1:-}" in
    refus_demarrage) refus_demarrage ;;
    migrations) migrations ;;
    attendre) attendre; exit $? ;;
    verifier) verifier; exit $? ;;
    portails) preparer_donnees; verifier_portails ;;
    securite) verifier_admin; verifier_hotes_inconnus; verifier_securite ;;
    resolver) resolver ;;
    certificat) certificat ;;
    *) echo "Usage : smoke_prod.sh refus_demarrage|migrations|attendre|verifier|portails|securite|resolver|certificat" >&2; exit 2 ;;
esac

if [ "${#ECHECS[@]}" -gt 0 ]; then exit 1; fi
