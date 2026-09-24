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
# Le check sur le préfixe 'django-insecure-' vise le placeholder connu de
# startproject, mais ne protège pas contre une future valeur par défaut
# différente dans base.py, ni contre un humain qui tape à la main une clé
# courte/faible en pensant bien faire ("motdepasse123", par exemple). La
# vérification de longueur ci-dessous est indépendante du texte exact :
# une vraie clé Django générée (get_random_secret_key()) fait 50
# caractères ; on exige un plancher légèrement en dessous (40) pour
# rester tolérant à une clé générée par un autre outil équivalent, tout
# en rejetant tout ce qui ressemble à un mot de passe humain ou un
# placeholder.
SECRET_KEY_LONGUEUR_MINIMALE = 40
if not SECRET_KEY or SECRET_KEY.startswith('django-insecure-') or len(SECRET_KEY) < SECRET_KEY_LONGUEUR_MINIMALE:
    raise ImproperlyConfigured(
        "SECRET_KEY doit être une vraie clé aléatoire d'au moins "
        f"{SECRET_KEY_LONGUEUR_MINIMALE} caractères en production, jamais "
        "vide, jamais le placeholder 'django-insecure-...' généré par "
        "startproject, et jamais une valeur courte tapée à la main. Cette "
        "clé signe entre autres les tokens JWT : une clé faible ou par "
        "défaut permet de forger des tokens valides pour n'importe quel "
        "compte. Générez-en une avec : "
        "python -c \"from django.core.management.utils import "
        "get_random_secret_key; print(get_random_secret_key())\""
    )

FIELD_ENCRYPTION_KEY = config('FIELD_ENCRYPTION_KEY', default='')
if not FIELD_ENCRYPTION_KEY or FIELD_ENCRYPTION_KEY == 'mKvbTFkFfRPhMpb4ZJdZfHvz1pgUgx15Xhyn1ahJCiw=':
    raise ImproperlyConfigured(
        "FIELD_ENCRYPTION_KEY doit être défini explicitement en production, "
        "différent de la clé de développement par défaut. Cette clé chiffre "
        "des données sensibles (ex: compte bancaire KYC) : la clé de dev "
        "étant publique (présente dans le dépôt), l'utiliser en production "
        "reviendrait à ne pas chiffrer du tout. Générez-en une avec : "
        "python -c \"from cryptography.fernet import Fernet; "
        "print(Fernet.generate_key().decode())\""
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

# F-18 (audit sécurité) : Django tourne derrière Gunicorn, lui-même
# uniquement joignable depuis le réseau Docker interne (aucun port publié
# sur l'hôte dans docker-compose.prod.yml) — seul Nginx peut lui parler.
# La requête qui arrive à Django est donc TOUJOURS du HTTP en interne,
# même quand le visiteur est bien en HTTPS ; sans ce réglage,
# SECURE_SSL_REDIRECT ci-dessus provoquerait une boucle de redirection
# infinie (Django croit que toute requête est en HTTP et redirige sans
# fin). Nginx pose lui-même l'en-tête X-Forwarded-Proto à sa propre valeur
# de $scheme (jamais à partir d'un en-tête client) sur les trois emplacements
# proxy_pass (/, /api/, /admin/, /fast/) — voir infra/nginx/nginx.conf — donc
# aucun client ne peut forger cet en-tête pour usurper du HTTPS auprès de
# Django tant que ce dernier reste inaccessible autrement que via Nginx.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# HSTS : force le navigateur à toujours utiliser HTTPS pour ce domaine, même
# si un lien ou un attaquant en position MITM essaie de forcer un retour en
# HTTP. Sans ça, SECURE_SSL_REDIRECT ne protège pas la toute première requête.
#
# Valeur de démarrage volontairement courte (1 jour) : à monter progressivement
# vers 31536000 (1 an) une fois le HTTPS validé en continu sur le domaine —
# un HSTS agressif mal configuré peut rendre le site inaccessible en HTTP
# pendant toute sa durée, y compris pour corriger un souci de certificat.
SECURE_HSTS_SECONDS = config('SECURE_HSTS_SECONDS', default=86400, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = config('SECURE_HSTS_INCLUDE_SUBDOMAINS', default=False, cast=bool)
SECURE_HSTS_PRELOAD = config('SECURE_HSTS_PRELOAD', default=False, cast=bool)