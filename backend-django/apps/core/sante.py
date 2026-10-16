"""Point de santé interne : /health/ (healthcheck du conteneur Django)."""
import logging

import redis
from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

logger = logging.getLogger(__name__)

# Délai court : un Redis injoignable ne doit pas bloquer un worker Gunicorn
# jusqu'à son timeout.
DELAI_REDIS = 2


def _base_disponible():
    with connection.cursor() as curseur:
        curseur.execute('SELECT 1')


def _redis_disponible():
    client = redis.Redis.from_url(
        settings.REDIS_URL,
        socket_connect_timeout=DELAI_REDIS,
        socket_timeout=DELAI_REDIS,
    )
    try:
        client.ping()
    finally:
        client.close()


@require_safe
@never_cache
def sante(request):
    """200 si la base et Redis répondent, 503 sinon.

    Route interne : nginx la ferme à l'extérieur (404) et les healthchecks
    compose l'appellent depuis le conteneur. Aucune authentification, et la
    réponse ne nomme jamais le composant en défaut (il est dans les journaux).
    """
    for nom, verifier in (('base de données', _base_disponible), ('redis', _redis_disponible)):
        try:
            verifier()
        except Exception:
            logger.exception('Healthcheck : %s indisponible', nom)
            return JsonResponse({'status': 'indisponible'}, status=503)
    return JsonResponse({'status': 'ok'})
