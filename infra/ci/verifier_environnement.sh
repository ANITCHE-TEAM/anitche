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

if [ "$ECHECS" -gt 0 ]; then
    echo "❌ $ECHECS variante(s) mal gérée(s) par check_prod_env.sh." >&2
    exit 1
fi
echo "✅ check_prod_env.sh : environnement valide accepté, variantes invalides refusées."
