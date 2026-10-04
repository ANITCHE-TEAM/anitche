#!/usr/bin/env bash
# infra/scripts/check_prod_env.sh
# Contrôle des variables de production avant un déploiement (deploy.sh le
# lance après avoir chargé infra/.env). Refuse tout ce que refusent au
# démarrage :
#   - config/settings/prod.py (Django, Celery) ;
#   - backend-fastapi/app/core/settings.py (ENVIRONMENT=prod) : origines CORS
#     et adresses publiques ;
#   - infra/postgres/fastapi_readonly.sql : mot de passe du rôle en lecture
#     seule de FastAPI ;
# plus les valeurs de infra/.env.example laissées telles quelles.
# Affiche toutes les erreurs, puis sort en 1. N'affiche aucune valeur : le
# nom de la variable et la règle seulement (un secret ne sort jamais).
# Avertit, sans bloquer, si infra/.env n'a pas les droits 600.
#
# Usage, depuis la racine du dépôt :
#   set -a; . infra/.env; set +a; bash infra/scripts/check_prod_env.sh

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BACKEND_EMAIL_SMTP='django.core.mail.backends.smtp.EmailBackend'
DOMAINE_EXPEDITEUR='anitche.com'
# Clé Fernet publique de config/settings/prod.py (CLE_DE_DEVELOPPEMENT).
CLE_DE_DEVELOPPEMENT='mKvbTFkFfRPhMpb4ZJdZfHvz1pgUgx15Xhyn1ahJCiw='
# Mot de passe de développement du rôle FastAPI (DEV_DATABASE_PASSWORD).
MOT_DE_PASSE_FASTAPI_DEV='fastapi_ro_dev'
MOTIF_IPV6='^\[([^]]*)](:(.*))?$'
MOTIF_NOM_AFFICHE='^[^<>]*<([^<>]*)>$'
MOTIF_CARACTERES_INTERDITS='[[:space:]<>,"]'

ERREURS=()
erreur() { ERREURS+=("$1"); }

# $1 sans espaces au début ni à la fin.
nettoyer() {
    local valeur="$1"
    valeur="${valeur#"${valeur%%[![:space:]]*}"}"
    printf '%s' "${valeur%"${valeur##*[![:space:]]}"}"
}

# Liste séparée par des virgules, sans entrées vides, dans ELEMENTS : même
# lecture que prod.py (ALLOWED_HOSTS, CORS_ALLOWED_ORIGINS) et FastAPI.
decouper_csv() {
    ELEMENTS=()
    local morceaux morceau
    IFS=',' read -r -a morceaux <<< "$1"
    for morceau in "${morceaux[@]}"; do
        morceau="$(nettoyer "$morceau")"
        [ -n "$morceau" ] && ELEMENTS+=("$morceau")
    done
}

# Booléen lu comme python-decouple : « vrai », « faux » ou « invalide ».
booleen() {
    case "${1,,}" in
        y|yes|t|true|on|1) echo vrai ;;
        n|no|f|false|off|0|'') echo faux ;;
        *) echo invalide ;;
    esac
}

port_valide() { [[ "$1" =~ ^[0-9]{1,5}$ ]] && (( 10#$1 <= 65535 )); }

# Hôtes refusés par FastAPI en production (LOCAL_HOSTS).
est_local() {
    case "$1" in
        localhost|127.0.0.1|::1|0.0.0.0) return 0 ;;
    esac
    return 1
}

# Découpe une URL comme urllib.parse.urlsplit : autorité jusqu'au premier
# « / », « ? » ou « # » ; hôte en minuscules, sans identifiants ni port ;
# chemin ; puis paramètres et fragment (URL_SUITE).
analyser_url() {
    local reste autorite hote_port
    URL_HOTE=''; URL_PORT=''; URL_A_PORT=0; URL_CHEMIN=''; URL_SUITE=''; URL_IDENTIFIANTS=0
    [[ "$1" == *://* ]] || return 0
    reste="${1#*://}"
    autorite="${reste%%[/?#]*}"
    reste="${reste:${#autorite}}"
    URL_CHEMIN="${reste%%[?#]*}"
    URL_SUITE="${reste:${#URL_CHEMIN}}"
    [[ "$autorite" == *@* ]] && URL_IDENTIFIANTS=1
    hote_port="${autorite##*@}"
    if [[ "$hote_port" =~ $MOTIF_IPV6 ]]; then
        URL_HOTE="${BASH_REMATCH[1]}"
        if [ -n "${BASH_REMATCH[2]}" ]; then URL_A_PORT=1; URL_PORT="${BASH_REMATCH[3]}"; fi
    elif [[ "$hote_port" == *:* ]]; then
        URL_HOTE="${hote_port%%:*}"; URL_PORT="${hote_port#*:}"; URL_A_PORT=1
    else
        URL_HOTE="$hote_port"
    fi
    URL_HOTE="${URL_HOTE,,}"
}

# Même règle que django.http.request.validate_host : « .exemple.com »
# accepte le domaine et ses sous-domaines, sinon égalité exacte, sans casse.
hote_autorise() {
    local motif
    for motif in "${HOTES_AUTORISES[@]}"; do
        motif="${motif,,}"
        if [[ "$motif" == .* ]]; then
            [[ "$1" == *"$motif" || "$1" == "${motif#.}" ]] && return 0
        elif [ "$1" = "$motif" ]; then
            return 0
        fi
    done
    return 1
}

# Adresse publique recopiée par FastAPI dans ses réponses : https://, hôte
# public, ni identifiants, ni paramètres, ni fragment, port valide.
verifier_url_publique() {
    local nom="$1" url="$2"
    if [[ "$url" != https://* ]]; then
        erreur "$nom doit commencer par https:// (adresse publique, FastAPI)."
        return
    fi
    analyser_url "$url"
    if [ -z "$URL_HOTE" ] || est_local "$URL_HOTE"; then
        erreur "$nom : hôte public attendu, ni vide ni localhost (FastAPI)."
    fi
    if [ "$URL_IDENTIFIANTS" = 1 ] || [ -n "$URL_SUITE" ]; then
        erreur "$nom : ni identifiants (@), ni paramètres (?), ni fragment (#) (FastAPI)."
    fi
    if [ "$URL_A_PORT" = 1 ] && ! port_valide "$URL_PORT"; then
        erreur "$nom : port invalide."
    fi
}

# --- Variables obligatoires -------------------------------------------------
REQUIS=(
    SECRET_KEY FIELD_ENCRYPTION_KEYS ALLOWED_HOSTS CORS_ALLOWED_ORIGINS
    DB_NAME DB_USER DB_PASSWORD FASTAPI_DB_PASSWORD
    PAIEMENT_FOURNISSEUR CINETPAY_API_KEY CINETPAY_API_PASSWORD BACKEND_BASE_URL
    PUBLIC_BASE_URL MEDIA_BASE_URL
    EMAIL_BACKEND EMAIL_HOST EMAIL_HOST_USER EMAIL_HOST_PASSWORD DEFAULT_FROM_EMAIL
)
for var in "${REQUIS[@]}"; do
    [ -n "$(nettoyer "${!var:-}")" ] || erreur "$var : absente ou vide."
done

# --- Secrets de Django (prod.py) --------------------------------------------
if [ -n "${SECRET_KEY:-}" ]; then
    if [[ "$SECRET_KEY" == django-insecure-* ]] || [ "${#SECRET_KEY}" -lt 40 ]; then
        erreur "SECRET_KEY : clé aléatoire de 40 caractères au moins, jamais « django-insecure-… » ni la valeur de .env.example."
    fi
fi

if [ -n "${FIELD_ENCRYPTION_KEYS:-}" ]; then
    decouper_csv "$FIELD_ENCRYPTION_KEYS"
    [ "${#ELEMENTS[@]}" -gt 0 ] || erreur "FIELD_ENCRYPTION_KEYS : aucune clé."
    n=0
    for cle in "${ELEMENTS[@]}"; do
        n=$((n + 1))
        if [ "$cle" = "$CLE_DE_DEVELOPPEMENT" ]; then
            erreur "FIELD_ENCRYPTION_KEYS : la clé n°$n est la clé de développement, publique."
        elif [[ ! "$cle" =~ ^[A-Za-z0-9_-]{43}=$ ]]; then
            erreur "FIELD_ENCRYPTION_KEYS : la clé n°$n n'est pas une clé Fernet (44 caractères base64 URL)."
        fi
    done
fi

# --- Hôtes, CORS et URL de notification des paiements -----------------------
HOTES_AUTORISES=()
if [ -n "${ALLOWED_HOSTS:-}" ]; then
    decouper_csv "$ALLOWED_HOSTS"
    HOTES_AUTORISES=("${ELEMENTS[@]}")
    [ "${#HOTES_AUTORISES[@]}" -gt 0 ] || erreur "ALLOWED_HOSTS : aucun hôte."
    [[ "$ALLOWED_HOSTS" == *"*"* ]] && erreur "ALLOWED_HOSTS : le joker « * » est interdit en production."
fi

if [ -n "${CORS_ALLOWED_ORIGINS:-}" ]; then
    decouper_csv "$CORS_ALLOWED_ORIGINS"
    [ "${#ELEMENTS[@]}" -gt 0 ] || erreur "CORS_ALLOWED_ORIGINS : aucune origine."
    n=0
    for origine in "${ELEMENTS[@]}"; do
        n=$((n + 1))
        analyser_url "$origine"
        if [[ "$origine" == *"*"* || "$origine" != https://* ]] || [ -z "$URL_HOTE" ] || est_local "$URL_HOTE"; then
            erreur "CORS_ALLOWED_ORIGINS : l'origine n°$n doit être en https://, sans « * » ni localhost (FastAPI)."
        fi
    done
fi

if [ -n "${BACKEND_BASE_URL:-}" ]; then
    if [[ "$BACKEND_BASE_URL" != https://* ]]; then
        erreur "BACKEND_BASE_URL doit être l'adresse https:// publique de l'API (URL de notification des paiements)."
    else
        analyser_url "$BACKEND_BASE_URL"
        if [ -z "$URL_HOTE" ] || ! hote_autorise "$URL_HOTE"; then
            erreur "BACKEND_BASE_URL : son hôte doit figurer dans ALLOWED_HOSTS (sinon Django rejette les notifications de paiement)."
        fi
        if [ -n "$URL_CHEMIN" ] || [ -n "$URL_SUITE" ]; then
            erreur "BACKEND_BASE_URL : origine seule, sans chemin ni « / » final (ex. https://anitche.com)."
        fi
    fi
fi

# --- Paiements (prod.py) ----------------------------------------------------
if [ -n "${PAIEMENT_FOURNISSEUR:-}" ] && [ "$PAIEMENT_FOURNISSEUR" != cinetpay ]; then
    erreur "PAIEMENT_FOURNISSEUR doit valoir cinetpay en production (le fournisseur simulé n'encaisse rien)."
fi
if [[ "${CINETPAY_API_KEY:-}" == sk_test_* ]]; then
    erreur "CINETPAY_API_KEY : clé de sandbox (sk_test_), interdite en production."
fi

# --- Emails (prod.py) -------------------------------------------------------
if [ -n "${EMAIL_BACKEND:-}" ] && [ "$EMAIL_BACKEND" != "$BACKEND_EMAIL_SMTP" ]; then
    erreur "EMAIL_BACKEND doit valoir $BACKEND_EMAIL_SMTP (console, locmem, dummy et filebased n'envoient rien)."
fi
# Valeurs par défaut identiques à celles de docker-compose.prod.yml.
tls="$(booleen "${EMAIL_USE_TLS:-True}")"
ssl="$(booleen "${EMAIL_USE_SSL:-False}")"
[ "$tls" != invalide ] || erreur "EMAIL_USE_TLS : booléen attendu (True ou False)."
[ "$ssl" != invalide ] || erreur "EMAIL_USE_SSL : booléen attendu (True ou False)."
if [ "$tls" = vrai ] && [ "$ssl" = vrai ]; then
    erreur "EMAIL_USE_TLS et EMAIL_USE_SSL sont exclusifs : port 587 avec STARTTLS (TLS) ou 465 avec TLS implicite (SSL)."
fi
if [ "$tls" = faux ] && [ "$ssl" = faux ]; then
    erreur "EMAIL_USE_TLS ou EMAIL_USE_SSL doit être activé : sans chiffrement, l'identifiant et la clé SMTP partent en clair."
fi
port_valide "${EMAIL_PORT:-587}" || erreur "EMAIL_PORT : numéro de port attendu (587 ou 465)."
delai="${EMAIL_TIMEOUT:-10}"
if [[ ! "$delai" =~ ^[0-9]{1,2}$ ]] || (( 10#$delai < 1 || 10#$delai > 60 )); then
    erreur "EMAIL_TIMEOUT : nombre entier de secondes entre 1 et 60."
fi
if [ -n "${DEFAULT_FROM_EMAIL:-}" ]; then
    expediteur="$(nettoyer "$DEFAULT_FROM_EMAIL")"
    # « ANITCHE <no-reply@anitche.com> » : seule l'adresse compte.
    if [[ "$expediteur" =~ $MOTIF_NOM_AFFICHE ]]; then adresse="${BASH_REMATCH[1]}"; else adresse="$expediteur"; fi
    partie_locale="${adresse%@*}"
    domaine="${adresse##*@}"
    if [[ "$adresse" != *@* || -z "$partie_locale" || "$partie_locale" == *@* || "${domaine,,}" != "$DOMAINE_EXPEDITEUR" ]] \
            || [[ "$adresse" =~ $MOTIF_CARACTERES_INTERDITS ]]; then
        erreur "DEFAULT_FROM_EMAIL : adresse @$DOMAINE_EXPEDITEUR attendue (ex. « ANITCHE <no-reply@anitche.com> », entre guillemets dans infra/.env)."
    fi
fi

# --- FastAPI : adresses publiques et rôle PostgreSQL en lecture seule -------
# FRONTEND_BASE_URL : même valeur par défaut que docker-compose.prod.yml.
verifier_url_publique FRONTEND_BASE_URL "${FRONTEND_BASE_URL:-https://anitche.com}"
[ -z "${PUBLIC_BASE_URL:-}" ] || verifier_url_publique PUBLIC_BASE_URL "$PUBLIC_BASE_URL"
[ -z "${MEDIA_BASE_URL:-}" ] || verifier_url_publique MEDIA_BASE_URL "$MEDIA_BASE_URL"

if [ -n "${FASTAPI_DB_PASSWORD:-}" ]; then
    if [ "$FASTAPI_DB_PASSWORD" = "$MOT_DE_PASSE_FASTAPI_DEV" ]; then
        erreur "FASTAPI_DB_PASSWORD : mot de passe de développement."
    else
        [ "${#FASTAPI_DB_PASSWORD}" -ge 12 ] \
            || erreur "FASTAPI_DB_PASSWORD : 12 caractères au moins (infra/postgres/fastapi_readonly.sql)."
        [[ "$FASTAPI_DB_PASSWORD" =~ ^[A-Za-z0-9]+$ ]] \
            || erreur "FASTAPI_DB_PASSWORD : lettres et chiffres uniquement (inséré tel quel dans l'URL de connexion de FastAPI)."
    fi
fi

# --- PostgreSQL ---------------------------------------------------------------
if [ -n "${DB_PASSWORD:-}" ]; then
    case "$DB_PASSWORD" in
        postgres|change-me-too) erreur "DB_PASSWORD : valeur d'exemple, à remplacer par un mot de passe aléatoire." ;;
        *) [ "${#DB_PASSWORD}" -ge 12 ] || erreur "DB_PASSWORD : 12 caractères au moins." ;;
    esac
fi

# --- Droits de infra/.env (avertissement seulement) -------------------------
FICHIER_ENV="$INFRA_DIR/.env"
if [ -f "$FICHIER_ENV" ]; then
    droits="$(stat -c '%a' "$FICHIER_ENV" 2>/dev/null || echo inconnus)"
    if [ "$droits" != 600 ]; then
        echo "⚠️  AVERTISSEMENT : infra/.env a les droits $droits ; 600 attendu (lecture et écriture pour son seul propriétaire) : chmod 600 infra/.env" >&2
    fi
else
    echo "⚠️  AVERTISSEMENT : infra/.env introuvable, droits non vérifiés." >&2
fi

if [ "${#ERREURS[@]}" -gt 0 ]; then
    for message in "${ERREURS[@]}"; do
        echo "❌ ERREUR : $message" >&2
    done
    echo "   ${#ERREURS[@]} erreur(s) : déploiement annulé. Corrigez infra/.env (modèle : infra/.env.example)." >&2
    exit 1
fi

echo "✅ Toutes les variables requises sont présentes et valides. Déploiement autorisé."
