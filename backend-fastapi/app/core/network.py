"""Adresse IP du client derrière nginx."""
import ipaddress

from starlette.requests import HTTPConnection


def client_ip(connection: HTTPConnection) -> str | None:
    """Adresse IP du client, ou None si elle est absente ou invalide.

    Même règle que DRF (`BaseThrottle.get_ident`, NUM_PROXIES) et que
    `apps/core/reseau.py` côté Django : X-Forwarded-For n'est lu que derrière
    un proxy de confiance déclaré (TRUSTED_PROXY_COUNT), jamais tel que le
    client l'envoie. En prod, nginx écrase X-Forwarded-For avec l'IP réelle
    du visiteur : on lit la dernière entrée. Valable pour HTTP et WebSocket.
    """
    trusted_proxy_count = connection.app.state.settings.trusted_proxy_count
    forwarded_for = ",".join(connection.headers.getlist("x-forwarded-for"))
    remote_addr = connection.client.host if connection.client else None

    if trusted_proxy_count > 0 and forwarded_for:
        addresses = forwarded_for.split(",")
        candidate = addresses[-min(trusted_proxy_count, len(addresses))].strip()
    else:
        candidate = remote_addr

    try:
        return str(ipaddress.ip_address(candidate))
    except (TypeError, ValueError):
        return None
