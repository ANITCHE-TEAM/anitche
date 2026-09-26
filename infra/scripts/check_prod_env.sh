#!/bin/bash
# infra/scripts/check_prod_env.sh
# À lancer avant tout déploiement prod — échoue bruyamment si une variable
# essentielle manque, plutôt que de laisser Django planter en silence
# ou pire, démarrer avec une valeur par défaut dangereuse.

set -e

REQUIS=("SECRET_KEY" "FIELD_ENCRYPTION_KEYS" "ALLOWED_HOSTS" "CORS_ALLOWED_ORIGINS" "DB_NAME" "DB_USER" "DB_PASSWORD" "PAIEMENT_FOURNISSEUR" "CINETPAY_API_KEY" "CINETPAY_API_PASSWORD" "BACKEND_BASE_URL")

for var in "${REQUIS[@]}"; do
    if [ -z "${!var}" ]; then
        echo "❌ ERREUR : la variable d'environnement $var n'est pas définie."
        echo "   Le déploiement est annulé pour éviter un démarrage avec une config incomplète."
        exit 1
    fi
done

if [[ "$ALLOWED_HOSTS" == *"*"* ]]; then
    echo "❌ ERREUR : ALLOWED_HOSTS contient un wildcard '*', interdit en production."
    exit 1
fi

if [ "$PAIEMENT_FOURNISSEUR" = "simule" ]; then
    echo "❌ ERREUR : PAIEMENT_FOURNISSEUR=simule est interdit en production (aucun argent réel)."
    exit 1
fi

if [[ "$CINETPAY_API_KEY" == sk_test_* ]]; then
    echo "❌ ERREUR : CINETPAY_API_KEY est une clé de sandbox (sk_test_), interdite en production."
    exit 1
fi

if [[ "$BACKEND_BASE_URL" != https://* ]]; then
    echo "❌ ERREUR : BACKEND_BASE_URL doit être l'adresse HTTPS publique de l'API."
    exit 1
fi

if [ "$SECRET_KEY" = "change-me-in-production" ] || [ "$DB_PASSWORD" = "change-me-too" ]; then
    echo "❌ ERREUR : une valeur placeholder de .env.example n'a pas été remplacée."
    echo "   Vérifiez infra/.env avant de déployer."
    exit 1
fi

echo "✅ Toutes les variables requises sont présentes et valides. Déploiement autorisé."