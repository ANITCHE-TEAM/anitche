import ipaddress

from rest_framework.throttling import BaseThrottle


def adresse_ip_client(request):
    """Adresse IP du client, ou None si elle est absente ou invalide.

    Même source que les limites de débit DRF (`BaseThrottle.get_ident`,
    piloté par REST_FRAMEWORK['NUM_PROXIES']) : un en-tête X-Forwarded-For
    n'est lu que derrière un proxy de confiance déclaré, jamais tel que
    le client l'envoie. La validation évite d'écrire une valeur arbitraire
    dans une colonne `inet` (erreur 500 sous PostgreSQL).
    """
    identifiant = BaseThrottle().get_ident(request)
    try:
        return str(ipaddress.ip_address(identifiant))
    except (TypeError, ValueError):
        return None
