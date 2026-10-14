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

# --- Paiements : GeniusPay, mode staging, CinetPay ----------------------------
# Valeurs factices. L'environnement de CI est celui de GeniusPay (live) et
# contient encore des clés CinetPay factices, requises par docker-compose :
# les scénarios GeniusPay les retirent pour prouver qu'elles ne sont pas
# exigées, les scénarios CinetPay fixent PAIEMENT_FOURNISSEUR=cinetpay.
SANS_CINETPAY=(-CINETPAY_API_KEY -CINETPAY_API_PASSWORD)
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
CP=PAIEMENT_FOURNISSEUR=cinetpay
scenario_accepte "geniuspay live (environnement de CI), variable staging absente"
scenario_accepte "cinetpay, variable staging absente" "$CP"
scenario_accepte "cinetpay, GeniusPay absent" "$CP" -GENIUSPAY_API_KEY -GENIUSPAY_API_SECRET -GENIUSPAY_WEBHOOK_SECRET -GENIUSPAY_ENVIRONNEMENT
scenario_accepte "cinetpay, variable staging à true (sans effet)" "$CP" "$STAGING=true"
scenario_accepte "cinetpay, variable staging à false" "$CP" "$STAGING=false"
scenario_accepte "geniuspay live, sans clés CinetPay" "${SANS_CINETPAY[@]}" "${GP_LIVE[@]}"
scenario_accepte "geniuspay live, variable staging à false" "${SANS_CINETPAY[@]}" "${GP_LIVE[@]}" "$STAGING=false"
scenario_accepte "geniuspay sandbox en staging" "${SANS_CINETPAY[@]}" "${GP_SANDBOX[@]}"

echo "==> Paiements : variantes invalides"
scenario_refuse CINETPAY_API_KEY "cinetpay sans CINETPAY_API_KEY" "$CP" -CINETPAY_API_KEY
scenario_refuse CINETPAY_API_PASSWORD "cinetpay avec CINETPAY_API_PASSWORD vide" "$CP" CINETPAY_API_PASSWORD=
scenario_refuse CINETPAY_API_KEY "cinetpay, clé sk_test_" "$CP" CINETPAY_API_KEY=sk_test_factice
scenario_refuse CINETPAY_API_KEY "cinetpay, clé sk_test_, variable staging à true" "$CP" CINETPAY_API_KEY=sk_test_factice "$STAGING=true"
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
    scenario_refuse "$STAGING" "cinetpay, variable staging « $valeur »" "$CP" "$STAGING=$valeur"
done

if [ "$ECHECS" -gt 0 ]; then
    echo "❌ $ECHECS variante(s) mal gérée(s) par check_prod_env.sh." >&2
    exit 1
fi
echo "✅ check_prod_env.sh : environnement valide accepté, variantes invalides refusées."
