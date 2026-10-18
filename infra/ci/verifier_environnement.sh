#!/usr/bin/env bash
# infra/ci/verifier_environnement.sh
# check_prod_env.sh sur l'environnement de CI : il doit passer, puis refuser
# chacune des variantes invalides ci-dessous, en citant la variable en cause
# (un contrôle devenu trop permissif, ou qui échoue pour une autre raison,
# fait échouer ce script). N'affiche aucune valeur de l'environnement.
#
# Usage, depuis la racine du dépôt : bash infra/ci/verifier_environnement.sh <fichier_env>

set -uo pipefail

FICHIER="${1:?Usage : verifier_environnement.sh <fichier_env>}"
SORTIE="$(mktemp)"
ECHECS=0

echo "==> Environnement de CI : check_prod_env.sh doit passer"
# shellcheck source=/dev/null
if ! ( set -a; . "$FICHIER"; set +a; bash infra/scripts/check_prod_env.sh ); then
    echo "❌ check_prod_env.sh refuse l'environnement de CI." >&2
    exit 1
fi

# Lance check_prod_env.sh sur l'environnement de CI modifié par les arguments :
# NOM=valeur pour définir (valeur vide comprise), -NOM pour retirer.
lancer_controle() {
    # shellcheck source=/dev/null
    ( set -a; . "$FICHIER"; set +a
      for modification in "$@"; do
          case "$modification" in
              -*) unset "${modification#-}" ;;
              *) export "${modification?}" ;;
          esac
      done
      bash infra/scripts/check_prod_env.sh ) >"$SORTIE" 2>&1
}

# $1 : variable, $2 : valeur invalide. Attendu : sortie 1 et le nom de la
# variable dans les erreurs.
variante() {
    local nom="$1" valeur="$2" code
    # shellcheck source=/dev/null
    ( set -a; . "$FICHIER"; set +a; export "$nom=$valeur"; bash infra/scripts/check_prod_env.sh ) >"$SORTIE" 2>&1
    code=$?
    if [ "$code" -ne 1 ]; then
        echo "❌ $nom invalide : check_prod_env.sh devait sortir en 1, code $code." >&2
        sed 's/^/   | /' "$SORTIE" >&2
        ECHECS=$((ECHECS + 1))
    elif ! grep -q "ERREUR : $nom" "$SORTIE"; then
        echo "❌ $nom invalide : refusé, mais sans erreur citant $nom." >&2
        sed 's/^/   | /' "$SORTIE" >&2
        ECHECS=$((ECHECS + 1))
    else
        echo "✅ $nom invalide : refusé."
    fi
}

echo "==> Variantes invalides : check_prod_env.sh doit les refuser"
variante PAIEMENT_FOURNISSEUR simule
variante EMAIL_BACKEND django.core.mail.backends.console.EmailBackend
variante EMAIL_HOST ""
variante DEFAULT_FROM_EMAIL "ANITCHE <no-reply@exemple.com>"
variante SECRET_KEY django-insecure-court
variante FIELD_ENCRYPTION_KEYS pas-une-cle
variante BACKUP_REPOSITORY /var/backups/anitche

# --- Paiements : GeniusPay, mode staging --------------------------------------
# Valeurs factices. L'environnement de CI est celui de GeniusPay (live).
GP_LIVE=(PAIEMENT_FOURNISSEUR=geniuspay GENIUSPAY_API_KEY=sk_live_factice GENIUSPAY_API_SECRET=ss_live_factice
         GENIUSPAY_WEBHOOK_SECRET=whsec_factice GENIUSPAY_ENVIRONNEMENT=live)
GP_SANDBOX=(PAIEMENT_FOURNISSEUR=geniuspay GENIUSPAY_API_KEY=sk_sandbox_factice GENIUSPAY_API_SECRET=ss_sandbox_factice
            GENIUSPAY_WEBHOOK_SECRET=whsec_factice GENIUSPAY_ENVIRONNEMENT=sandbox
            ANITCHE_STAGING_SANDBOX_PAIEMENT=true)
STAGING=ANITCHE_STAGING_SANDBOX_PAIEMENT

# $1 : libellé, puis les modifications. Attendu : sortie 0.
scenario_accepte() {
    local libelle="$1" code
    shift
    lancer_controle "$@"
    code=$?
    if [ "$code" -ne 0 ]; then
        echo "❌ $libelle : accepté attendu, code $code." >&2
        sed 's/^/   | /' "$SORTIE" >&2
        ECHECS=$((ECHECS + 1))
    else
        echo "✅ $libelle : accepté."
    fi
}

# $1 : variable citée dans l'erreur, $2 : libellé, puis les modifications.
# Attendu : sortie 1, le nom de la variable dans les erreurs, aucun secret.
scenario_refuse() {
    local nom="$1" libelle="$2" code
    shift 2
    lancer_controle "$@"
    code=$?
    if [ "$code" -ne 1 ]; then
        echo "❌ $libelle : refus attendu, code $code." >&2
        sed 's/^/   | /' "$SORTIE" >&2
        ECHECS=$((ECHECS + 1))
    elif ! grep -q "ERREUR : $nom" "$SORTIE"; then
        echo "❌ $libelle : refusé, mais sans erreur citant $nom." >&2
        sed 's/^/   | /' "$SORTIE" >&2
        ECHECS=$((ECHECS + 1))
    elif grep -q -E "whsec_factice|whsec_ci_factice|ss_live_factice|ss_live_ci_factice|ss_sandbox_factice" "$SORTIE"; then
        echo "❌ $libelle : refusé, mais l'erreur affiche un secret." >&2
        ECHECS=$((ECHECS + 1))
    else
        echo "✅ $libelle : refusé."
    fi
}

echo "==> Paiements : configurations valides"
scenario_accepte "geniuspay live (environnement de CI), variable staging absente"
scenario_accepte "geniuspay live" "${GP_LIVE[@]}"
scenario_accepte "geniuspay live, variable staging à false" "${GP_LIVE[@]}" "$STAGING=false"
scenario_accepte "geniuspay sandbox en staging" "${GP_SANDBOX[@]}"

echo "==> Paiements : variantes invalides"
scenario_refuse PAIEMENT_FOURNISSEUR "fournisseur cinetpay (hors liste)" "${GP_LIVE[@]}" PAIEMENT_FOURNISSEUR=cinetpay
scenario_refuse PAIEMENT_FOURNISSEUR "fournisseur cinetpay, variable staging à true" "${GP_SANDBOX[@]}" PAIEMENT_FOURNISSEUR=cinetpay
scenario_refuse PAIEMENT_FOURNISSEUR "fournisseur inconnu" PAIEMENT_FOURNISSEUR=stripe
scenario_refuse PAIEMENT_FOURNISSEUR "fournisseur geniuspay en casse différente" "${GP_LIVE[@]}" PAIEMENT_FOURNISSEUR=GeniusPay
for nom in GENIUSPAY_API_KEY GENIUSPAY_API_SECRET GENIUSPAY_WEBHOOK_SECRET; do
    scenario_refuse "$nom" "geniuspay, $nom absente" "${GP_LIVE[@]}" "-$nom"
    scenario_refuse "$nom" "geniuspay, $nom vide" "${GP_LIVE[@]}" "$nom="
    scenario_refuse "$nom" "geniuspay, $nom en espaces" "${GP_LIVE[@]}" "$nom=  "
done
scenario_refuse GENIUSPAY_ENVIRONNEMENT "geniuspay, environnement absent" "${GP_LIVE[@]}" -GENIUSPAY_ENVIRONNEMENT
scenario_refuse GENIUSPAY_ENVIRONNEMENT "geniuspay, environnement vide" "${GP_LIVE[@]}" GENIUSPAY_ENVIRONNEMENT=
scenario_refuse GENIUSPAY_ENVIRONNEMENT "geniuspay, environnement « production »" "${GP_LIVE[@]}" GENIUSPAY_ENVIRONNEMENT=production
scenario_refuse GENIUSPAY_ENVIRONNEMENT "geniuspay, environnement « LIVE »" "${GP_LIVE[@]}" GENIUSPAY_ENVIRONNEMENT=LIVE
scenario_refuse GENIUSPAY_API_KEY "geniuspay live, clé publique de sandbox" "${GP_LIVE[@]}" GENIUSPAY_API_KEY=sk_sandbox_factice
scenario_refuse GENIUSPAY_API_SECRET "geniuspay live, clé secrète de sandbox" "${GP_LIVE[@]}" GENIUSPAY_API_SECRET=ss_sandbox_factice
scenario_refuse GENIUSPAY_API_KEY "geniuspay live, préfixe sk_test_" "${GP_LIVE[@]}" GENIUSPAY_API_KEY=sk_test_factice
scenario_refuse GENIUSPAY_API_SECRET "geniuspay live, clé secrète sans préfixe" "${GP_LIVE[@]}" GENIUSPAY_API_SECRET=factice
scenario_refuse GENIUSPAY_API_KEY "geniuspay live, clés inversées" "${GP_LIVE[@]}" GENIUSPAY_API_KEY=ss_live_factice GENIUSPAY_API_SECRET=sk_live_factice
# Préfixes de la documentation GeniusPay (pk_ / sk_), que le tableau de bord ne délivre pas.
scenario_refuse GENIUSPAY_API_KEY "geniuspay live, clé publique pk_live_" "${GP_LIVE[@]}" GENIUSPAY_API_KEY=pk_live_factice
scenario_refuse GENIUSPAY_API_SECRET "geniuspay live, clé secrète sk_live_" "${GP_LIVE[@]}" GENIUSPAY_API_SECRET=sk_live_autre
scenario_refuse GENIUSPAY_API_KEY "geniuspay sandbox, clé publique pk_sandbox_" "${GP_SANDBOX[@]}" GENIUSPAY_API_KEY=pk_sandbox_factice
scenario_refuse GENIUSPAY_API_SECRET "geniuspay sandbox, clé secrète sk_sandbox_" "${GP_SANDBOX[@]}" GENIUSPAY_API_SECRET=sk_sandbox_autre
scenario_refuse GENIUSPAY_ENVIRONNEMENT "geniuspay sandbox, variable staging absente" "${GP_SANDBOX[@]}" "-$STAGING"
scenario_refuse GENIUSPAY_ENVIRONNEMENT "geniuspay sandbox, variable staging à false" "${GP_SANDBOX[@]}" "$STAGING=false"
scenario_refuse GENIUSPAY_ENVIRONNEMENT "geniuspay sandbox, clés live, variable staging absente" "${GP_LIVE[@]}" GENIUSPAY_ENVIRONNEMENT=sandbox
scenario_refuse "$STAGING" "geniuspay live, variable staging à true" "${GP_LIVE[@]}" "$STAGING=true"
scenario_refuse "$STAGING" "geniuspay live avec clés de sandbox, variable staging à true" "${GP_SANDBOX[@]}" GENIUSPAY_ENVIRONNEMENT=live
scenario_refuse GENIUSPAY_API_KEY "geniuspay sandbox, clé publique live" "${GP_SANDBOX[@]}" GENIUSPAY_API_KEY=sk_live_factice
scenario_refuse GENIUSPAY_API_SECRET "geniuspay sandbox, clé secrète live" "${GP_SANDBOX[@]}" GENIUSPAY_API_SECRET=ss_live_factice
for valeur in True TRUE 1 yes on "" " " "true " " true" False 0 oui; do
    scenario_refuse "$STAGING" "geniuspay, variable staging « $valeur »" "${GP_SANDBOX[@]}" "$STAGING=$valeur"
done

echo "==> Redis et PostgreSQL : variantes invalides"
scenario_refuse REDIS_PASSWORD "REDIS_PASSWORD absente" -REDIS_PASSWORD
scenario_refuse REDIS_PASSWORD "REDIS_PASSWORD vide" REDIS_PASSWORD=
scenario_refuse REDIS_PASSWORD "REDIS_PASSWORD de 31 caractères" REDIS_PASSWORD=0123456789abcdef0123456789abcde
scenario_refuse REDIS_PASSWORD "REDIS_PASSWORD avec « / »" REDIS_PASSWORD=0123456789abcdef/0123456789abcdef
scenario_refuse REDIS_PASSWORD "REDIS_PASSWORD avec « @ »" REDIS_PASSWORD=0123456789abcdef@0123456789abcdef
scenario_refuse REDIS_PASSWORD "REDIS_PASSWORD avec « : »" REDIS_PASSWORD=0123456789abcdef:0123456789abcdef
scenario_refuse REDIS_PASSWORD "REDIS_PASSWORD avec une espace" "REDIS_PASSWORD=0123456789abcdef 0123456789abcdef"
scenario_refuse DB_SUPERUSER_PASSWORD "DB_SUPERUSER_PASSWORD absente" -DB_SUPERUSER_PASSWORD
scenario_refuse DB_SUPERUSER_PASSWORD "DB_SUPERUSER_PASSWORD courte" DB_SUPERUSER_PASSWORD=court
scenario_refuse DB_SUPERUSER_PASSWORD "DB_SUPERUSER_PASSWORD « postgres »" DB_SUPERUSER_PASSWORD=postgres
scenario_refuse DB_SUPERUSER_PASSWORD "DB_SUPERUSER_PASSWORD égal à DB_PASSWORD" DB_SUPERUSER_PASSWORD=identique12345 DB_PASSWORD=identique12345
scenario_refuse DB_PASSWORD "DB_PASSWORD égal à FASTAPI_DB_PASSWORD" DB_PASSWORD=identique12345 FASTAPI_DB_PASSWORD=identique12345

echo "==> Domaines et nginx : configurations valides"
scenario_accepte "ADMIN_ALLOWED_CIDRS vide (/admin/ fermé à tous)" ADMIN_ALLOWED_CIDRS=
scenario_accepte "ADMIN_ALLOWED_CIDRS absente" -ADMIN_ALLOWED_CIDRS
scenario_accepte "ADMIN_ALLOWED_CIDRS : IPv4, IPv6, avec et sans préfixe" \
    "ADMIN_ALLOWED_CIDRS=203.0.113.7, 198.51.100.0/24,2001:db8::/48,2001:db8::1"
scenario_accepte "sous-domaines des portails explicites" \
    DOMAIN_VENDEUR=vendeurs.staging.exemple.test DOMAIN_LIVREUR=livraison.exemple.test DOMAIN_ADMIN=gestion.exemple.test
scenario_accepte "NGINX_RELOAD_INTERVAL=12h" NGINX_RELOAD_INTERVAL=12h

echo "==> Domaines et nginx : variantes invalides"
scenario_refuse DOMAIN "DOMAIN absente" -DOMAIN
scenario_refuse DOMAIN "DOMAIN avec schéma" DOMAIN=https://staging.exemple.test
scenario_refuse DOMAIN "DOMAIN avec « / » final" DOMAIN=staging.exemple.test/
scenario_refuse DOMAIN "DOMAIN avec port" DOMAIN=staging.exemple.test:443
scenario_refuse DOMAIN "DOMAIN en majuscules" DOMAIN=Staging.Exemple.test
scenario_refuse DOMAIN "DOMAIN sans point" DOMAIN=localhost
scenario_refuse DOMAIN "DOMAIN avec « ; » (injection dans nginx)" "DOMAIN=exemple.test;return"
scenario_refuse DOMAIN "DOMAIN absent de ALLOWED_HOSTS" DOMAIN=autre.exemple.test
scenario_refuse DOMAIN_VENDEUR "DOMAIN_VENDEUR avec espace" "DOMAIN_VENDEUR=vendeur exemple.test"
scenario_refuse DOMAIN_ADMIN "DOMAIN_ADMIN avec schéma" DOMAIN_ADMIN=https://admin.exemple.test
for valeur in all 0.0.0.0/0 ::/0 "203.0.113.7; allow all" 203.0.113 256.1.1.1 203.0.113.0/33 2001:db8::/129 "*"; do
    scenario_refuse ADMIN_ALLOWED_CIDRS "ADMIN_ALLOWED_CIDRS « $valeur »" "ADMIN_ALLOWED_CIDRS=$valeur"
done
for valeur in 0 " 6h" "6 h" 6heures -1h 1.5h; do
    scenario_refuse NGINX_RELOAD_INTERVAL "NGINX_RELOAD_INTERVAL « $valeur »" "NGINX_RELOAD_INTERVAL=$valeur"
done

if [ "$ECHECS" -gt 0 ]; then
    echo "❌ $ECHECS variante(s) mal gérée(s) par check_prod_env.sh." >&2
    exit 1
fi
echo "✅ check_prod_env.sh : environnement valide accepté, variantes invalides refusées."
