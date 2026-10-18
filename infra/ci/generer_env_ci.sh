#!/usr/bin/env bash
# infra/ci/generer_env_ci.sh
# Écrit le fichier d'environnement de la pile de production pour la CI.
# Les valeurs secrètes sont tirées au hasard à chaque appel et n'existent que
# le temps du job ; les autres sont des constantes manifestement fausses
# (domaines .invalid et .test, « factice »). Les clés GeniusPay n'ont que le préfixe
# attendu en production (sk_live_, ss_live_), suivi de « ci_factice ».
# Sous GitHub Actions, chaque valeur aléatoire est masquée dans les journaux.
#
# DOMAIN (staging.exemple.test par défaut, domaine réservé aux tests) :
# toute la pile tourne sur ce domaine, ce qui prouve qu'aucun nom n'est écrit
# en dur. Seul DEFAULT_FROM_EMAIL reste en @anitche.com, domaine d'envoi
# imposé par prod.py. /admin/ n'est ouvert qu'à la boucle locale : refusé
# depuis l'hôte, servi depuis le conteneur nginx (smoke_prod.sh).
#
# Usage : bash infra/ci/generer_env_ci.sh <fichier_de_sortie>

set -euo pipefail

SORTIE="${1:?Usage : generer_env_ci.sh <fichier_de_sortie>}"
DOMAIN="${DOMAIN:-staging.exemple.test}"

masquer() {
    if [ -n "${GITHUB_ACTIONS:-}" ]; then echo "::add-mask::$1"; fi
}

SECRET_KEY="$(openssl rand -hex 32)"
# Clé Fernet : 32 octets aléatoires en base64 URL.
FIELD_ENCRYPTION_KEYS="$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
DB_PASSWORD="$(openssl rand -hex 16)"
DB_SUPERUSER_PASSWORD="$(openssl rand -hex 16)"
FASTAPI_DB_PASSWORD="$(openssl rand -hex 16)"
REDIS_PASSWORD="$(openssl rand -hex 32)"
BACKUP_PASSWORD="$(openssl rand -hex 32)"

for valeur in "$SECRET_KEY" "$FIELD_ENCRYPTION_KEYS" "$DB_PASSWORD" "$DB_SUPERUSER_PASSWORD" "$FASTAPI_DB_PASSWORD"               "$REDIS_PASSWORD" "$BACKUP_PASSWORD"; do
    masquer "$valeur"
done

umask 077
cat > "$SORTIE" <<ENV
DOMAIN=$DOMAIN
ADMIN_ALLOWED_CIDRS=127.0.0.1/32,::1/128
SECRET_KEY=$SECRET_KEY
FIELD_ENCRYPTION_KEYS=$FIELD_ENCRYPTION_KEYS
ALLOWED_HOSTS=$DOMAIN,www.$DOMAIN,backend-django
CORS_ALLOWED_ORIGINS=https://$DOMAIN
FRONTEND_BASE_URL=https://$DOMAIN
DB_NAME=anitche_ci
DB_PASSWORD=$DB_PASSWORD
DB_SUPERUSER_PASSWORD=$DB_SUPERUSER_PASSWORD
FASTAPI_DB_PASSWORD=$FASTAPI_DB_PASSWORD
REDIS_PASSWORD=$REDIS_PASSWORD
PUBLIC_BASE_URL=https://$DOMAIN/fast
MEDIA_BASE_URL=https://$DOMAIN/media/
PAIEMENT_FOURNISSEUR=geniuspay
GENIUSPAY_API_KEY=sk_live_ci_factice
GENIUSPAY_API_SECRET=ss_live_ci_factice
GENIUSPAY_WEBHOOK_SECRET=whsec_ci_factice
GENIUSPAY_ENVIRONNEMENT=live
BACKEND_BASE_URL=https://$DOMAIN
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
