from email.utils import parseaddr
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.http.request import validate_host

from decouple import Csv

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

# Aucune documentation d'API en production (décision d'équipe) : valeur
# figée ici, jamais lue dans l'environnement. Vérifié par un test permanent.
DOCUMENTATION_API_ACTIVE = False

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

# Clés de chiffrement des données KYC (apps.core.fields, MultiFernet) :
# FIELD_ENCRYPTION_KEYS (clé active en premier), ou à défaut la clé unique
# FIELD_ENCRYPTION_KEY. Transmises par docker-compose.prod.yml.
CLE_DE_DEVELOPPEMENT = 'mKvbTFkFfRPhMpb4ZJdZfHvz1pgUgx15Xhyn1ahJCiw='
FIELD_ENCRYPTION_KEY = config('FIELD_ENCRYPTION_KEY', default='')
FIELD_ENCRYPTION_KEYS = config('FIELD_ENCRYPTION_KEYS', default='', cast=Csv()) or (
    [FIELD_ENCRYPTION_KEY] if FIELD_ENCRYPTION_KEY else []
)
if not FIELD_ENCRYPTION_KEYS or CLE_DE_DEVELOPPEMENT in FIELD_ENCRYPTION_KEYS:
    raise ImproperlyConfigured(
        "FIELD_ENCRYPTION_KEYS doit être défini explicitement en production, "
        "sans la clé de développement par défaut. Ces clés chiffrent des données "
        "sensibles (numéro mobile money du KYC, numéro de versement) : la clé de dev "
        "étant publique (présente dans le dépôt), l'utiliser en production "
        "reviendrait à ne pas chiffrer du tout. Générez-en une avec : "
        "python -c \"from cryptography.fernet import Fernet; "
        "print(Fernet.generate_key().decode())\""
    )
try:
    from cryptography.fernet import Fernet

    for _cle in FIELD_ENCRYPTION_KEYS:
        Fernet(_cle.encode())
except ValueError as erreur:
    raise ImproperlyConfigured(
        "FIELD_ENCRYPTION_KEYS contient une clé invalide (clé Fernet attendue : "
        f"32 octets encodés en base64 URL) : {erreur}"
    )

# Paiements : jamais le fournisseur simulé (il accepte des notifications
# signées avec un secret de développement public), jamais une clé CinetPay
# de sandbox, jamais sans clés. Transmis par docker-compose.prod.yml.
if PAIEMENT_FOURNISSEUR == 'simule':
    raise ImproperlyConfigured(
        "PAIEMENT_FOURNISSEUR='simule' est interdit en production : définissez "
        "PAIEMENT_FOURNISSEUR=cinetpay et ses clés (docs/MODULE_PAIEMENTS.md)."
    )
if PAIEMENT_FOURNISSEUR == 'cinetpay':
    if not CINETPAY_API_KEY or not CINETPAY_API_PASSWORD:
        raise ImproperlyConfigured(
            "CINETPAY_API_KEY et CINETPAY_API_PASSWORD doivent être définis en production."
        )
    if CINETPAY_API_KEY.startswith('sk_test_'):
        raise ImproperlyConfigured(
            "Clé CinetPay de sandbox (sk_test_) interdite en production : aucun paiement ne serait réel."
        )
if not BACKEND_BASE_URL.startswith('https://'):
    raise ImproperlyConfigured(
        "BACKEND_BASE_URL doit être l'adresse HTTPS publique de l'API (URL de notification des paiements)."
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

# L'URL de notification CinetPay (apps.paiements.fournisseurs.cinetpay) est
# f"{BACKEND_BASE_URL}/api/paiements/webhook/...". Elle n'aboutit que si son
# hôte est accepté par Django (sinon 400 DisallowedHost, et aucun paiement
# n'est jamais confirmé) et si BACKEND_BASE_URL est l'origine seule : nginx
# n'envoie à Django que les chemins qui commencent par /api/ (un préfixe
# « /x/api/... » part vers le frontend), et un « / » final produirait
# « //api/... », que seule la fusion des « / » par nginx rattrape.
_url_backend = urlsplit(BACKEND_BASE_URL)
if not _url_backend.hostname or not validate_host(_url_backend.hostname, ALLOWED_HOSTS):
    raise ImproperlyConfigured(
        f"L'hôte de BACKEND_BASE_URL ({_url_backend.hostname!r}) doit figurer dans "
        "ALLOWED_HOSTS : sinon Django rejette les notifications de paiement (400)."
    )
if _url_backend.path or _url_backend.query or _url_backend.fragment:
    raise ImproperlyConfigured(
        "BACKEND_BASE_URL doit être l'origine seule, sans chemin ni « / » final "
        "(ex. https://anitche.com) : le chemin /api/... est ajouté par le code."
    )

if not CORS_ALLOWED_ORIGINS:
    raise ImproperlyConfigured(
        "CORS_ALLOWED_ORIGINS doit être défini explicitement en production "
        "(ex: 'https://anitche.ci,https://admin.anitche.ci')."
    )

# Emails (codes OTP d'inscription, de mot de passe oublié et de changement de
# contact, notifications) : un vrai serveur SMTP authentifié, sinon ces
# parcours sont inutilisables sans que rien ne le signale. EMAIL_HOST est relu
# sans la valeur par défaut de base.py : une variable absente est refusée,
# jamais remplacée par un serveur tiers. Transmis par docker-compose.prod.yml.
BACKEND_EMAIL_SMTP = 'django.core.mail.backends.smtp.EmailBackend'
DOMAINE_EXPEDITEUR = 'anitche.com'
EMAIL_HOST = config('EMAIL_HOST', default='')

if EMAIL_BACKEND != BACKEND_EMAIL_SMTP:
    raise ImproperlyConfigured(
        f"EMAIL_BACKEND doit valoir '{BACKEND_EMAIL_SMTP}' en production (reçu : "
        f"{EMAIL_BACKEND!r}) : les backends console, locmem, dummy et filebased "
        "n'envoient rien, aucun code OTP n'arriverait."
    )
_email_manquants = [
    nom for nom, valeur in (
        ('EMAIL_HOST', EMAIL_HOST),
        ('EMAIL_HOST_USER', EMAIL_HOST_USER),
        ('EMAIL_HOST_PASSWORD', EMAIL_HOST_PASSWORD),
    ) if not valeur.strip()
]
if _email_manquants:
    raise ImproperlyConfigured(
        f"{', '.join(_email_manquants)} : à définir en production (serveur SMTP "
        "et identifiants SMTP du fournisseur d'emails)."
    )
if EMAIL_USE_TLS and EMAIL_USE_SSL:
    raise ImproperlyConfigured(
        "EMAIL_USE_TLS et EMAIL_USE_SSL sont exclusifs : STARTTLS sur le port 587 "
        "(EMAIL_USE_TLS=True) ou TLS implicite sur le port 465 (EMAIL_USE_SSL=True)."
    )
if not EMAIL_USE_TLS and not EMAIL_USE_SSL:
    raise ImproperlyConfigured(
        "EMAIL_USE_TLS ou EMAIL_USE_SSL doit être activé : sans chiffrement, "
        "l'identifiant et la clé SMTP partent en clair sur le réseau."
    )
if not 1 <= EMAIL_TIMEOUT <= 60:
    raise ImproperlyConfigured(
        "EMAIL_TIMEOUT doit être compris entre 1 et 60 secondes : sans limite, un "
        "serveur SMTP qui ne répond plus bloque un worker Celery."
    )
# Le fournisseur n'accepte que les expéditeurs du domaine authentifié (SPF,
# DKIM, DMARC) ; tout autre domaine est refusé ou classé en spam.
# « ANITCHE <no-reply@anitche.com> » est accepté : seule l'adresse compte.
_local, _, _domaine = parseaddr(DEFAULT_FROM_EMAIL)[1].rpartition('@')
if not _local or '@' in _local or _domaine.lower() != DOMAINE_EXPEDITEUR:
    raise ImproperlyConfigured(
        f"DEFAULT_FROM_EMAIL doit être une adresse @{DOMAINE_EXPEDITEUR} (reçu : "
        f"{DEFAULT_FROM_EMAIL!r} ; ex. « ANITCHE <no-reply@anitche.com> »)."
    )
# Expéditeur des emails d'erreur de Django (mail_admins) : la même adresse
# vérifiée chez le fournisseur, jamais root@localhost.
SERVER_EMAIL = DEFAULT_FROM_EMAIL

# Aucune credential cross-origin (cookies de session) n'est nécessaire :
# l'API s'authentifie par JWT dans l'en-tête Authorization, jamais par
# cookie. Le laisser à False évite qu'un futur CORS_ALLOW_ALL_ORIGINS
# ou une origine trop large ne devienne exploitable via des cookies.
CORS_ALLOW_CREDENTIALS = False

SECURE_SSL_REDIRECT = True
# Seule exception à la redirection HTTPS : la route interne de vérification
# du jeton, que FastAPI appelle directement sur le réseau Docker
# (http://backend-django:8000), sans passer par nginx ni TLS. Sans cette
# ligne, l'appel reçoit un 301 vers https et l'authentification FastAPI
# échoue. SecurityMiddleware retire le « / » initial du chemin avant de
# tester ces motifs, d'où « ^api/ » et non « ^/api/ ». Depuis Internet, nginx
# répond 404 sur ce chemin sans le transmettre à Django (location
# /api/utilisateurs/jeton/verification dans infra/nginx/nginx.conf).
SECURE_REDIRECT_EXEMPT = [r'^api/utilisateurs/jeton/verification/$']
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

# Adresse IP du client (limites de débit DRF, apps.core.reseau) : un seul
# proxy de confiance, Nginx. Il résout lui-même l'IP réelle du visiteur
# (module real_ip, uniquement pour les connexions venant des plages
# Cloudflare) puis ÉCRASE X-Forwarded-For avec cette seule valeur — voir
# infra/nginx/nginx.conf. Django lit donc la dernière (et unique) entrée.
# Même raisonnement que SECURE_PROXY_SSL_HEADER : valable tant que Django
# n'est joignable que via Nginx.
REST_FRAMEWORK['NUM_PROXIES'] = 1

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