from django.core.exceptions import ImproperlyConfigured

from .base import *


def _parse_liste_env(valeur):
    """
    Transforme une variable d'environnement CSV ('a.com, b.com')
    en liste propre, sans entrées vides ni espaces parasites.

    Volontairement strict : mieux vaut une erreur explicite au
    démarrage qu'un ALLOWED_HOSTS=[''] silencieux qui pousse
    quelqu'un à "réparer" en production avec un wildcard '*'.
    """
    return [item.strip() for item in valeur.split(',') if item.strip()]


DEBUG = False

SECRET_KEY = config('SECRET_KEY', default='')
if not SECRET_KEY or SECRET_KEY.startswith('django-insecure-'):
    raise ImproperlyConfigured(
        "SECRET_KEY doit être une vraie clé aléatoire en production, "
        "jamais le placeholder 'django-insecure-...' généré par "
        "startproject. Cette clé signe entre autres les tokens JWT : "
        "une clé faible ou par défaut permet de forger des tokens "
        "valides pour n'importe quel compte. Générez-en une avec : "
        "python -c \"from django.core.management.utils import "
        "get_random_secret_key; print(get_random_secret_key())\""
    )

ALLOWED_HOSTS = _parse_liste_env(config('ALLOWED_HOSTS', default=''))
CORS_ALLOWED_ORIGINS = _parse_liste_env(config('CORS_ALLOWED_ORIGINS', default=''))

if not ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "ALLOWED_HOSTS doit être défini explicitement en production "
        "(ex: 'api.anitche.ci,anitche.ci'). Un wildcard '*' est interdit : "
        "il expose à l'injection de Host header (cache poisoning, liens "
        "de réinitialisation de mot de passe forgés, etc.)."
    )

if '*' in ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        "ALLOWED_HOSTS='*' est interdit en production."
    )

if not CORS_ALLOWED_ORIGINS:
    raise ImproperlyConfigured(
        "CORS_ALLOWED_ORIGINS doit être défini explicitement en production "
        "(ex: 'https://anitche.ci,https://admin.anitche.ci')."
    )

# Aucune credential cross-origin (cookies de session) n'est nécessaire :
# l'API s'authentifie par JWT dans l'en-tête Authorization, jamais par
# cookie. Le laisser à False évite qu'un futur CORS_ALLOW_ALL_ORIGINS
# ou une origine trop large ne devienne exploitable via des cookies.
CORS_ALLOW_CREDENTIALS = False

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Headers de sécurité supplémentaires (OWASP baseline)
SECURE_HSTS_SECONDS = 31536000  # 1 an — force HTTPS pour les visites futures
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
SECURE_BROWSER_XSS_FILTER = True