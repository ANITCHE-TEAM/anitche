from .base import *

DEBUG = True
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Désactiver le throttling en mode test pour éviter les 429 tout en conservant les scopes
# Désactiver le throttling en mode test pour éviter les 429 tout en conservant les scopes
REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'] = {
    'anon': '100000/day',
    'user': '100000/day',
    'otp': '100000/day',
    'login': '100000/day',
    'paiements': '100000/day',
    'support': '100000/day',
    'kyc': '100000/day',
    'boutique_creation': '100000/day',
    'commande_validation': '100000/day',
}
# Cache en mémoire rapide pour les tests
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    }
}


# Secrets de test fixes pour la vérification de signature des webhooks
# de paiement — jamais utilisés en dehors de l'environnement de test.
WEBHOOK_SECRETS = {
    'wave': 'secret-test-wave',
    'orange_money': 'secret-test-orange',
    'mtn_money': 'secret-test-mtn',
    'moov_money': 'secret-test-moov',
}