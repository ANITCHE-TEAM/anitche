from .base import *


DEBUG = True

# Documentation d'API (/api/schema/, /api/docs/, /api/redoc/) : libre en dev.
DOCUMENTATION_API_ACTIVE = True
ALLOWED_HOSTS = ['localhost', '127.0.0.1', 'anitche-backend']

# Autorisé uniquement en dev : pas de CORS_ALLOW_CREDENTIALS, donc pas
# de cookie de session exposé cross-origin. En prod, voir prod.py qui
# exige une CORS_ALLOWED_ORIGINS explicite et interdit le wildcard.
CORS_ALLOW_ALL_ORIGINS = True

# Emails de dev → Mailpit (infra/docker-compose.yml) : rien ne sort vers
# l'extérieur, tout se lit sur http://localhost:8025. Dans Docker, le
# compose fournit EMAIL_HOST=mailpit ; hors Docker, Mailpit doit écouter sur
# localhost:1025 (ou définir EMAIL_BACKEND=...console.EmailBackend).
EMAIL_BACKEND = config('EMAIL_BACKEND', default='django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = config('EMAIL_HOST', default='localhost')
EMAIL_PORT = config('EMAIL_PORT', default=1025, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=False, cast=bool)
EMAIL_HOST_USER = ''
EMAIL_HOST_PASSWORD = ''

# Limites de débit relevées en DEV UNIQUEMENT, pour que les Runs Postman
# (connexions, codes OTP, inscriptions à chaque exécution) ne se bloquent
# pas. prod, ci et test ne chargent jamais ce fichier ; les vraies valeurs
# restent celles de base.py (vérifié par TauxDeLimiteParEnvironnementTests
# et LimitesDeProductionTests).
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    'DEFAULT_THROTTLE_RATES': {
        **REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'],
        'login': '1000/hour',
        'otp_envoi': '1000/hour',
        'otp_verification': '1000/hour',
        'inscription': '1000/hour',
        # Une exécution de postman_paiements.json initie ou annule une dizaine
        # de paiements par client (20/heure en production).
        'paiements': '1000/hour',
        # postman_retours.json et postman_support.json créent plusieurs
        # demandes, tickets et fichiers par exécution.
        'retour_creation': '1000/hour',
        'retour_photo': '1000/hour',
        'support_ticket': '1000/hour',
        'support_message': '1000/hour',
        'support_piece_jointe': '1000/hour',
    },
}

# Paiements en dev : fournisseur simulé, secret PUBLIC (repris par la
# collection postman_paiements.json pour signer ses notifications). Aucun
# argent ne circule ; prod.py refuse ce fournisseur.
PAIEMENT_SIMULE_SECRET = config('PAIEMENT_SIMULE_SECRET', default='dev-simulation-anitche')
