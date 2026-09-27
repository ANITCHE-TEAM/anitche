import atexit
import shutil
import tempfile
from .base import *
from pathlib import Path

DEBUG = True

# Commande seed_demo testée par apps/demo/tests.py (jamais installée en production).
INSTALLED_APPS = [*INSTALLED_APPS, 'apps.demo']

# Routes de documentation montées pour les tests du schéma (config/tests_schema.py).
DOCUMENTATION_API_ACTIVE = True

# Fichiers écrits par les tests (pièces KYC, logos, images produit,
# pièces jointes...) : dossier temporaire propre à chaque exécution,
# supprimé à la fin. Sans ceci, chaque lancement de la suite déposait des
# dizaines de faux documents dans le vrai media/ (mêlés aux données de dev).
MEDIA_ROOT = tempfile.mkdtemp(prefix='anitche-tests-media-')
atexit.register(shutil.rmtree, MEDIA_ROOT, ignore_errors=True)
PASSWORD_HASHERS = [
    'django.contrib.auth.hashers.MD5PasswordHasher',
]

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': Path(tempfile.gettempdir()) / 'anitche_test_db.sqlite3',
        'TEST': {
            'NAME': Path(tempfile.gettempdir()) / 'anitche_test_db.sqlite3',
        },
        'OPTIONS': {
            'timeout': 20,
        },
    }
}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Désactiver le throttling en mode test pour éviter les 429 tout en conservant les scopes
# Désactiver le throttling en mode test pour éviter les 429 tout en conservant les scopes
REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'] = {
    'anon': '100000/day',
    'user': '100000/day',
    'otp_envoi': '100000/day',
    'otp_verification': '100000/day',
    'inscription': '100000/day',
    'rafraichissement': '100000/day',
    'login': '100000/day',
    'paiements': '100000/day',
    'webhook_paiement': '100000/day',
    'support': '100000/day',
    'kyc': '100000/day',
    'boutique_creation': '100000/day',
    'commande_validation': '100000/day',
    'commande_simulation': '100000/day',
    'coupon_verification': '100000/day',
    'fidelite_conversion': '100000/day',
    'logout': '100000/day',
    'passeport_verification': '100000/day',
    'catalogue_public': '100000/day',
    'livraison_statut': '100000/day',
    'livraison_contestation': '100000/day',
    'retour_creation': '100000/day',
    'retour_photo': '100000/day',
    'support_ticket': '100000/day',
    'support_message': '100000/day',
    'support_piece_jointe': '100000/day',
}
# Cache en mémoire rapide pour les tests
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    }
}


# Paiements : fournisseur simulé avec un secret de test fixe (jamais utilisé
# hors tests). Les tests CinetPay remplacent les appels HTTP.
PAIEMENT_FOURNISSEUR = 'simule'
PAIEMENT_SIMULE_SECRET = 'secret-test-simulation'
CINETPAY_API_KEY = ''
CINETPAY_API_PASSWORD = ''
BACKEND_BASE_URL = 'https://api.anitche.test'
