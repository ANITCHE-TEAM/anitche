#!/usr/bin/env bash
# infra/ci/etape.sh
# Exécute une commande et ajoute une ligne « étape | durée | résultat » au
# résumé du job (GITHUB_STEP_SUMMARY). Code de sortie : celui de la commande.
#
# Usage : bash infra/ci/etape.sh "<titre>" <commande> [arguments...]

TITRE="${1:?Usage : etape.sh \"<titre>\" <commande> [arguments...]}"
shift

debut="$(date +%s)"
if "$@"; then statut=0; resultat="✅"; else statut=$?; resultat="❌"; fi
duree=$(( $(date +%s) - debut ))

echo "==> $TITRE : ${duree} s ($resultat)"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    printf '| %s | %s s | %s |\n' "$TITRE" "$duree" "$resultat" >> "$GITHUB_STEP_SUMMARY"
fi
exit "$statut"
