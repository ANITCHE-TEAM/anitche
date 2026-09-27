"""
Settings utilisées par la CI GitHub Actions (ci-django.yml).

Hérite de test.py (mêmes throttles désactivés, même cache mémoire,
mêmes secrets de test) mais remplace le SQLite local par PostgreSQL,
pointé sur le service `postgres` du workflow.

RAISON : plusieurs comportements applicatifs (verrouillage de lignes
via select_for_update(), certaines contraintes) diffèrent réellement
entre SQLite et PostgreSQL — qui est la base cible de production (voir
CLAUDE.md). Faire tourner la suite de tests contre SQLite uniquement
laisse ces chemins non couverts en CI. test.py reste inchangé pour les
runs locaux rapides (aucune dépendance à un service Postgres).
"""
from decouple import config

from .test import *  # noqa: F401,F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config('DB_NAME', default='anitche_test'),
        'USER': config('DB_USER', default='postgres'),
        'PASSWORD': config('DB_PASSWORD', default='postgres'),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
    }
}
