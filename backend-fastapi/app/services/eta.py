"""Distance et temps restants, INDICATIFS.

Calculés seulement si le client a donné son point GPS au checkout
(`GroupeCommande.livraison_latitude/longitude`). Sans point : (None, None),
jamais une valeur inventée (pas de centre de commune, pas de destination
par défaut).

Distance à vol d'oiseau (Haversine) × facteur de détour, vitesse moyenne
urbaine sans trafic en temps réel : un ordre de grandeur, pas un itinéraire.
Réglages : ETA_DETOUR_FACTOR, ETA_AVERAGE_SPEED_KMH (app/core/settings.py).
"""
import math

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    # min : un arrondi flottant au-delà de 1 ferait échouer asin.
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def estimate(
    latitude: float,
    longitude: float,
    destination: tuple[float, float] | None,
    *,
    detour_factor: float,
    average_speed_kmh: float,
) -> tuple[float | None, int | None]:
    """(distance restante en km à 1 décimale, minutes entières >= 1), ou
    (None, None) sans point de destination ou si le calcul n'est pas fini."""
    if destination is None:
        return None, None
    distance_km = haversine_km(latitude, longitude, *destination) * detour_factor
    if not math.isfinite(distance_km):
        return None, None
    minutes = max(1, math.ceil(distance_km / average_speed_kmh * 60))
    return round(distance_km, 1), minutes
