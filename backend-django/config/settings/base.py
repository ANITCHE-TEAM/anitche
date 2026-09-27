import os
from pathlib import Path
from decouple import Csv, config
from celery.schedules import crontab

# Build paths inside the project like this: BASE_DIR / 'subdir'.
# ATTENTION : un niveau plus profond que l'ancien settings.py (config/settings/base.py)
BASE_DIR = Path(__file__).resolve().parent.parent.parent


# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY', default='django-insecure-%%j)4t(2o%i8!+7!()j7^matm=8*7a62^x*gsf0y=9kwhbr&lp')


# Application definition

AUTH_USER_MODEL = 'utilisateurs.Utilisateur'

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Tiers
    'rest_framework',
    'rest_framework_simplejwt',
    'rest_framework_simplejwt.token_blacklist',
    'corsheaders',
    'django_celery_beat',
    'drf_spectacular',

    # Apps métier (apps/<nom>)
    'apps.utilisateurs',
    'apps.catalogue',
    'apps.panier',
    'apps.commandes',
    'apps.paiements',
    'apps.livraison',
    'apps.retours',
    'apps.fidelite',
    'apps.vendeurs',
    'apps.notifications',
    'apps.support',
    'apps.passeport_qr',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database — PostgreSQL
# https://docs.djangoproject.com/en/6.0/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config('DB_NAME', default='anitche'),
        'USER': config('DB_USER', default='postgres'),
        'PASSWORD': config('DB_PASSWORD', default=''),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
    }
}


# Password validation
# https://docs.djangoproject.com/en/6.0/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# Internationalization
# https://docs.djangoproject.com/en/6.0/topics/i18n/

LANGUAGE_CODE = 'fr-fr'
TIME_ZONE = 'Africa/Abidjan'
USE_I18N = True
USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'
# Ça reste dans base.py et non dev.py : la config en elle-même (où pointent les fichiers) ne change pas entre dev et prod, seule la façon de servir ces fichiers change (voir étape 2). En prod, c'est un serveur web (nginx, S3...) qui prendra le relais — pas Django.



DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# Security Headers
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'

# Django REST Framework

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_THROTTLE_CLASSES': (
        'rest_framework.throttling.UserRateThrottle',
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.ScopedRateThrottle',
    ),
    'DEFAULT_THROTTLE_RATES': {
        'user': '300/hour',
        'anon': '50/hour',
        # Codes OTP : envoi (changement de contact, mot de passe oublié,
        # renvoi du code d'inscription) et vérification comptés à part —
        # un compteur commun bloquait la vérification après quelques
        # demandes de code. Chaque code reste limité à 5 essais (CodeOTP).
        'otp_envoi': '5/hour',
        'otp_verification': '10/hour',
        # Inscription, par IP : borne l'énumération des emails inscrits.
        'inscription': '10/hour',
        # Rafraîchissement du jeton, par IP (CGNAT : ~4 rafraîchissements
        # par heure et par session active).
        'rafraichissement': '300/hour',
        'login': '10/hour',
        'kyc': '5/hour',
        'boutique_creation': '10/hour',
        'commande_validation': '20/hour',
        # Simulation du checkout (frais de livraison, total à payer) : le
        # client la relance à chaque changement d'adresse ou de coupon. Elle
        # accepte un code promo : limite dédiée, sinon elle servirait à
        # essayer des codes hors de la limite de la validation.
        'commande_simulation': '120/hour',
        # Fidélité (A04:2025 / A07:2025) : sans limite dédiée, ces deux
        # endpoints ne dépendaient que du taux générique 'user' (300/heure).
        # coupon_verification borne le bourrinage de codes au hasard sur
        # VerifierCouponView (l'espace de codes générés reste très grand,
        # mais un taux dédié plus bas est une défense en profondeur peu
        # coûteuse) ; fidelite_conversion limite ConvertirPointsEnCouponView,
        # une opération financière (débit réel de points), au même ordre de
        # grandeur que commande_validation.
        'coupon_verification': '30/hour',
        'fidelite_conversion': '20/hour',
        # LogoutView est en AllowAny (posséder le refresh token suffit,
        # voir apps/utilisateurs/views.py) : taux dédié pour éviter que ce
        # point d'entrée public serve de vecteur de spam/DoS low-cost.
        'logout': '30/hour',
        # Vérification publique d'un passeport QR (par IP pour un visiteur).
        # Plus large que 'anon' : derrière le CGNAT des opérateurs mobiles,
        # des dizaines de clients d'un même quartier ou d'un même marché
        # partagent une IP publique. 600/h (10 scans/min en moyenne) reste
        # négligeable face aux 4 milliards de codes possibles par an.
        'passeport_verification': '600/hour',
        # Catalogue public (listes, fiches, catégories), par IP pour un
        # visiteur : même raison CGNAT, et une navigation normale enchaîne
        # beaucoup plus de requêtes qu'un scan (20/min en moyenne).
        'catalogue_public': '1200/hour',
        # Paiements, par utilisateur : initiation et annulation d'un
        # paiement en attente (un checkout normal en consomme 1 à 3).
        'paiements': '20/hour',
        # Notifications des fournisseurs de paiement, par IP : elles
        # arrivent toutes des mêmes serveurs. Avant, la limite anonyme
        # (50/heure) refusait des paiements réels au-delà de 50 par heure.
        # Chaque notification reste authentifiée puis revérifiée.
        'webhook_paiement': '3000/hour',
        # Livraison, par utilisateur : changements de statut (un livreur en
        # fait 4 par colis ; la saisie du code est en plus bornée à 5 essais
        # par code) et contestations « non reçu » du client.
        'livraison_statut': '120/hour',
        'livraison_contestation': '10/hour',
        # Retours, par utilisateur : demandes (un client en fait rarement
        # plus d'une par commande) et photos justificatives (5 par demande).
        'retour_creation': '10/hour',
        'retour_photo': '30/hour',
        # Support, par utilisateur : tickets, messages d'un fil de discussion
        # et pièces jointes (5 par message au plus).
        'support_ticket': '10/hour',
        'support_message': '60/hour',
        'support_piece_jointe': '20/hour',
    },
    # Sans cette ligne, config/exceptions.py::custom_exception_handler
    # n'est jamais appelé : les 500 utilisent le handler DRF par défaut.
    'EXCEPTION_HANDLER': 'config.exceptions.custom_exception_handler',
    # Schéma OpenAPI (drf-spectacular) : tags par module et réponses
    # d'erreur au format commun, voir config/schema.py.
    'DEFAULT_SCHEMA_CLASS': 'config.schema.SchemaAnitche',
    # A04:2025 (Unrestricted Resource Consumption) : sans pagination par
    # défaut, chaque ListAPIView du projet renvoie l'intégralité des
    # résultats en une requête — trivialement coûteux dès que le catalogue
    # ou l'annuaire de boutiques grossit, et facilite un scraping complet
    # en un seul appel sur les endpoints publics (ProduitPublicListView,
    # BoutiquePubliqueListView). CategorieListView reste volontairement
    # non paginée (pagination_class = None) car sa liste est petite par
    # nature et ordonnée pour l'affichage.
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    # Nombre de proxys de confiance devant Django. Laissé à None (défaut
    # DRF), l'identifiant des limites de débit anonymes est l'en-tête
    # X-Forwarded-For entier, que le client écrit lui-même : changer cet
    # en-tête à chaque requête contournait toutes les limites (login, otp,
    # logout...). 0 = aucun proxy : seul REMOTE_ADDR compte (dev, tests).
    # En production, prod.py le passe à 1 (Nginx, qui réécrit l'en-tête).
    'NUM_PROXIES': 0,
}

# Documentation d'API interactive (drf-spectacular) : /api/schema/,
# /api/docs/ (Swagger UI), /api/redoc/. Montée seulement si
# DOCUMENTATION_API_ACTIVE (dev.py, test.py) ; prod.py la force à False
# (décision d'équipe : aucune documentation exposée en production). Le
# schéma versionné (backend-django/schema.yaml) reste la référence du
# frontend, vérifié par la CI.
DOCUMENTATION_API_ACTIVE = False

SPECTACULAR_SETTINGS = {
    'TITLE': 'API ANITCHE',
    'DESCRIPTION': (
        "API REST du backend Django d'ANITCHE (marketplace, Abidjan, montants en FCFA entiers).\n\n"
        "**Authentification** : JWT. `POST /api/utilisateurs/connexion/` renvoie `access` (15 minutes) "
        "et `refresh` (7 jours, à usage unique : `POST /api/utilisateurs/connexion/rafraichir/` en "
        "renvoie un nouveau). Envoyer `Authorization: Bearer <access>` (bouton Authorize).\n\n"
        "**Erreurs** : format commun `{success, status_code, detail, errors}` (composant `Erreur`), "
        "`errors` étant un objet clé → liste de messages.\n\n"
        "**Pagination** : les listes paginées renvoient `{count, next, previous, results}` "
        "(20 éléments par page, paramètre `page`).\n\n"
        "Guide complet : docs/GUIDE_FRONTEND.md."
    ),
    'VERSION': '1.0.0',
    'OAS_VERSION': '3.1.0',
    'SERVE_INCLUDE_SCHEMA': False,
    # Composants distincts en lecture et en écriture (ex. Produit /
    # ProduitRequest) : champs en lecture seule absents des corps de
    # requête, multipart correctement typé, types TypeScript exacts.
    'COMPONENT_SPLIT_REQUEST': True,
    'SCHEMA_PATH_PREFIX': r'/api/',
    'TAGS': [
        {'name': 'Utilisateurs', 'description': "Inscription, connexion JWT (et Google), profil, codes OTP (email), mot de passe oublié, dépôt et lecture du dossier KYC."},
        {'name': 'Vendeurs', 'description': "Boutiques publiques, boutique du vendeur connecté, décisions sur les demandes vendeur et suspension des boutiques (administration)."},
        {'name': 'Catalogue', 'description': "Catégories et produits publics ; espace vendeur : produits, variantes, stock et images ; modération (administration)."},
        {'name': 'Panier', 'description': "Panier du client connecté ou anonyme (cookie de session) : articles, quantités, disponibilité."},
        {'name': 'Commandes', 'description': "Simulation et validation du checkout (une commande par boutique), suivi, annulation ; espace vendeur ; annulation par l'administration."},
        {'name': 'Paiements', 'description': "Paiement en ligne (mobile money, carte), annulation d'un paiement en attente ; reversements vendeur ; remboursements, reversements et barèmes de frais (administration). Les notifications des fournisseurs (webhooks) ne figurent pas ici."},
        {'name': 'Livraison', 'description': "Tarifs par commune, suivi selon le rôle (client, livreur, vendeur, administration), code de livraison, contestation, assignation et gestion des livreurs."},
        {'name': 'Retours', 'description': "Demandes de retour (commande livrée, 7 jours), photos justificatives, traitement par la boutique ou l'administration."},
        {'name': 'Fidélité', 'description': "Compte de points, gains en attente, transactions, conversion de points en coupon, vérification d'un code promo."},
        {'name': 'Notifications', 'description': "Notifications de l'utilisateur connecté, compteur de non lues, préférences d'envoi par email."},
        {'name': 'Support', 'description': "Tickets, messages, pièces jointes et notation ; file et assignation pour l'équipe support."},
        {'name': 'Passeports QR', 'description': "Passeports d'authenticité : vérification publique d'un QR, gestion par le vendeur."},
    ],
    # Noms stables des énumérations dont le nom de champ revient dans
    # plusieurs modèles (sans eux : StatusC4bEnum, Statut8cbEnum...).
    'ENUM_NAME_OVERRIDES': {
        'StatutCommandeEnum': 'apps.commandes.models.Commande.Status',
        'StatutLivraisonEnum': 'apps.livraison.models.Livraison.Status',
        'StatutContestationEnum': 'apps.livraison.models.ContestationLivraison.Statut',
        'StatutPaiementEnum': 'apps.paiements.models.Paiement.Statut',
        'StatutRemboursementEnum': 'apps.paiements.models.Remboursement.Statut',
        'MotifRemboursementEnum': 'apps.paiements.models.Remboursement.Motif',
        'StatutReversementEnum': 'apps.paiements.models.Reversement.Statut',
        'CanalReversementEnum': 'apps.paiements.models.Reversement.Canal',
        'StatutRetourEnum': 'apps.retours.models.DemandeRetour.Statut',
        'MotifRetourEnum': 'apps.retours.models.DemandeRetour.Motif',
        'StatutGainFideliteEnum': 'apps.fidelite.models.GainFidelite.Statut',
        'StatutTicketEnum': 'apps.support.models.SupportTicket.Status',
        'CanalNotificationEnum': 'apps.notifications.models.Notification.Canal',
    },
    # Versions figées (drf-spectacular charge sinon « @latest » depuis le
    # CDN) : même interface pour toute l'équipe, pas de mise à jour subie.
    'SWAGGER_UI_DIST': 'https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.33.0',
    'SWAGGER_UI_FAVICON_HREF': 'https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.33.0/favicon-32x32.png',
    'REDOC_DIST': 'https://cdn.jsdelivr.net/npm/redoc@2.5.4',
    'SWAGGER_UI_SETTINGS': {
        'persistAuthorization': True,
        'displayRequestDuration': True,
        'filter': True,
    },
}

from datetime import timedelta

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=15),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,

    # Invalide automatiquement tous les tokens déjà émis (access ET
    # refresh, ce dernier transmettant son claim à l'access token créé
    # à partir de lui) dès que le mot de passe de l'utilisateur change
    # — reset actuel, ou tout futur changement de mot de passe "connecté".
    # Sans ceci, un access token volé avant un changement de mot de passe
    # reste valable jusqu'à son expiration naturelle malgré le changement.
    'CHECK_REVOKE_TOKEN': True,
}


# Celery + Redis

CELERY_BROKER_URL = config('REDIS_URL', default='redis://localhost:6379/0')
CELERY_RESULT_BACKEND = config('REDIS_URL', default='redis://localhost:6379/0')

# Tâches planifiées (django-celery-beat, DatabaseScheduler — voir
# config/celery.py). Déclarées ici plutôt que via une data migration
# créant des PeriodicTask : la planification reste versionnée avec le
# reste de la config, lisible au même endroit, et modifiable sans
# nouvelle migration. Le DatabaseScheduler synchronise ces entrées en
# base au démarrage de `celery beat`.
CELERY_BEAT_SCHEDULE = {
    'utilisateurs-nettoyer-otp-expires': {
        'task': 'apps.utilisateurs.tasks.nettoyer_otp_expires',
        'schedule': crontab(hour=3, minute=0),
    },
    # Commandes non payées dans le délai : annulées, stock restitué.
    'commandes-expirer-commandes-non-payees': {
        'task': 'apps.commandes.tasks.expirer_commandes_non_payees',
        'schedule': crontab(minute='*/5'),
    },
    # Délai de rétractation écoulé : reversements aux vendeurs disponibles.
    'paiements-rendre-reversements-disponibles': {
        'task': 'apps.paiements.tasks.rendre_reversements_disponibles',
        'schedule': crontab(minute=10),
    },
    # Même délai écoulé : points de fidélité en attente crédités (apps.fidelite).
    'fidelite-crediter-points-echus': {
        'task': 'apps.fidelite.tasks.crediter_points_echus',
        'schedule': crontab(minute=15),
    },
    'utilisateurs-purger-tokens-expires': {
        'task': 'apps.utilisateurs.tasks.purger_tokens_expires',
        'schedule': crontab(hour=3, minute=15),
    },
}

# Cache Redis
REDIS_URL = os.environ.get('REDIS_URL', 'redis://redis:6379/0')
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': REDIS_URL.replace('/0', '/1'),
        'TIMEOUT': 300,  # 5 minutes
    }
}

# Auth Google
GOOGLE_OAUTH_CLIENT_ID = config('GOOGLE_OAUTH_CLIENT_ID', default='')

# Chiffrement au repos de champs sensibles (DocumentKYC.numero_mobile_money,
# Reversement.numero_destinataire)
# via apps.core.fields.EncryptedCharField. Valeur de dev par défaut non
# secrète et volontairement présente uniquement ici — jamais utilisée en
# prod (voir le raise dans prod.py) : elle sert juste à ce que
# `manage.py runserver` fonctionne sans configuration locale préalable.
# Générée avec Fernet.generate_key() ; à définir en variable
# d'environnement propre à chaque environnement (dev/staging/prod), jamais
# committée pour un environnement réel.
FIELD_ENCRYPTION_KEY = config(
    'FIELD_ENCRYPTION_KEY',
    default='mKvbTFkFfRPhMpb4ZJdZfHvz1pgUgx15Xhyn1ahJCiw='
)
# Rotation (MultiFernet) : liste de clés séparées par des virgules, la clé
# ACTIVE (qui chiffre) en premier, les anciennes ensuite (elles ne servent
# plus qu'à déchiffrer). Vide = FIELD_ENCRYPTION_KEY seule. Procédure :
# docs/MODULE_UTILISATEURS.md, « Gestion de la clé ».
FIELD_ENCRYPTION_KEYS = config('FIELD_ENCRYPTION_KEYS', default='', cast=Csv())

# Paiements (apps.paiements, docs/MODULE_PAIEMENTS.md).
# Fournisseur actif : 'simule' (dev, tests ; refusé en production) ou
# 'cinetpay'. Aucun secret n'a de valeur par défaut ici : ils viennent de
# l'environnement (docker-compose.prod.yml), comme FIELD_ENCRYPTION_KEYS.
PAIEMENT_FOURNISSEUR = config('PAIEMENT_FOURNISSEUR', default='simule')
# Secret HMAC des notifications du fournisseur simulé (dev.py en fournit un).
PAIEMENT_SIMULE_SECRET = config('PAIEMENT_SIMULE_SECRET', default='')
# CinetPay (API v1) : identifiants du compte marchand Côte d'Ivoire. L'URL
# de l'API se déduit du préfixe de la clé (sk_test_ → sandbox).
CINETPAY_API_KEY = config('CINETPAY_API_KEY', default='')
CINETPAY_API_PASSWORD = config('CINETPAY_API_PASSWORD', default='')
CINETPAY_API_URL = config('CINETPAY_API_URL', default='')
CINETPAY_TIMEOUT = config('CINETPAY_TIMEOUT', default=10, cast=int)
# Adresse publique de l'API, pour les URL de notification envoyées au
# fournisseur (120 caractères au plus chez CinetPay).
BACKEND_BASE_URL = config('BACKEND_BASE_URL', default='http://localhost:8000')
# Reversement au vendeur : N jours après la livraison confirmée.
REVERSEMENT_DELAI_RETRACTATION_JOURS = config('REVERSEMENT_DELAI_RETRACTATION_JOURS', default=7, cast=int)
# Retours : une demande est possible jusqu'à N jours après la livraison
# (apps.retours). Même valeur que le délai de rétractation par défaut.
RETOUR_DELAI_JOURS = config('RETOUR_DELAI_JOURS', default=7, cast=int)
# Livraison : passages « en cours » possibles (première tentative comprise)
# avant l'abandon par l'administration (commande annulée, remboursement).
LIVRAISON_TENTATIVES_MAX = config('LIVRAISON_TENTATIVES_MAX', default=3, cast=int)

# Adresse publique du frontend, utilisée pour construire les liens qui y
# mènent (ex : URL de vérification encodée dans le QR d'un passeport,
# apps.passeport_qr). Défaut = serveur Vite de dev ; en production,
# docker-compose.prod.yml la fournit. Domaine définitif en attente de
# confirmation par l'équipe (docs/MODULE_PASSEPORT_QR.md).
# Délai de paiement d'une commande (mobile money, carte) avant annulation
# automatique et restitution du stock (apps.commandes.tasks). Toute commande
# se paie en ligne : aucune exception (plus de paiement à la livraison).
COMMANDE_DELAI_PAIEMENT_MINUTES = config('COMMANDE_DELAI_PAIEMENT_MINUTES', default=30, cast=int)

FRONTEND_BASE_URL = config('FRONTEND_BASE_URL', default='http://localhost:5173')



#  Email config

EMAIL_BACKEND = config('EMAIL_BACKEND', default='django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = config('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='no-reply@anitche.com')


# Journalisation
#
# Sort sur stdout/stderr (pratique standard en conteneur : l'orchestrateur
# ou le service de logs agrège depuis là, pas besoin de gérer des fichiers
# et leur rotation nous-mêmes).
#
# Un logger dédié 'securite' capture les événements sensibles (tentatives
# de connexion sur compte désactivé, échecs répétés d'OTP, etc.) pour
# permettre la détection d'anomalies / alerting, séparément du bruit
# habituel de Django.

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{asctime} {levelname} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'securite': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}