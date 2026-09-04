from .base import *


DEBUG = True
ALLOWED_HOSTS = ['localhost', '127.0.0.1', 'anitche-backend']

# Autorisé uniquement en dev : pas de CORS_ALLOW_CREDENTIALS, donc pas
# de cookie de session exposé cross-origin. En prod, voir prod.py qui
# exige une CORS_ALLOWED_ORIGINS explicite et interdit le wildcard.
CORS_ALLOW_ALL_ORIGINS = True