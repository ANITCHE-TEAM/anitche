#!/usr/bin/env bash
# infra/ci/generer_env_ci.sh
# Écrit le fichier d'environnement de la pile de production pour la CI.
# Les valeurs secrètes sont tirées au hasard à chaque appel et n'existent que
# le temps du job ; les autres sont des constantes manifestement fausses
# (domaines .invalid, « factice »). Aucune valeur ne ressemble à une clé réelle.
# Sous GitHub Actions, chaque valeur aléatoire est masquée dans les journaux.
#
# Usage : bash infra/ci/generer_env_ci.sh <fichier_de_sortie>

set -euo pipefail

SORTIE="${1:?Usage : generer_env_ci.sh <fichier_de_sortie>}"

masquer() {
    if [ -n "${GITHUB_ACTIONS:-}" ]; then echo "::add-mask::$1"; fi
}

SECRET_KEY="$(openssl rand -hex 32)"
# Clé Fernet : 32 octets aléatoires en base64 URL.
FIELD_ENCRYPTION_KEYS="$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
DB_PASSWORD="$(openssl rand -hex 16)"
FASTAPI_DB_PASSWORD="$(openssl rand -hex 16)"
BACKUP_PASSWORD="$(openssl rand -hex 32)"

for valeur in "$SECRET_KEY" "$FIELD_ENCRYPTION_KEYS" "$DB_PASSWORD" "$FASTAPI_DB_PASSWORD" "$BACKUP_PASSWORD"; do
    masquer "$valeur"
done

umask 077
cat > "$SORTIE" <<ENV
SECRET_KEY=$SECRET_KEY
FIELD_ENCRYPTION_KEYS=$FIELD_ENCRYPTION_KEYS
ALLOWED_HOSTS=anitche.com,www.anitche.com,backend-django
CORS_ALLOWED_ORIGINS=https://anitche.com
FRONTEND_BASE_URL=https://anitche.com
DB_NAME=anitche_ci
DB_USER=anitche_ci
DB_PASSWORD=$DB_PASSWORD
FASTAPI_DB_PASSWORD=$FASTAPI_DB_PASSWORD
PUBLIC_BASE_URL=https://anitche.com/fast
MEDIA_BASE_URL=https://anitche.com/media/
PAIEMENT_FOURNISSEUR=cinetpay
CINETPAY_API_KEY=ci-cinetpay-cle-factice
CINETPAY_API_PASSWORD=ci-cinetpay-mdp-factice
BACKEND_BASE_URL=https://anitche.com
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.ci.invalid
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_USE_SSL=False
EMAIL_TIMEOUT=10
EMAIL_HOST_USER=ci-factice
EMAIL_HOST_PASSWORD=ci-factice-mdp
DEFAULT_FROM_EMAIL="ANITCHE <no-reply@anitche.com>"
BACKUP_REPOSITORY=s3:https://s3.ci.invalid/anitche-ci
BACKUP_PASSWORD=$BACKUP_PASSWORD
BACKUP_S3_ACCESS_KEY_ID=ci-factice-acces
BACKUP_S3_SECRET_ACCESS_KEY=ci-factice-secret
ENV

echo "Environnement de CI écrit dans $SORTIE ($(wc -l < "$SORTIE") variables)."
