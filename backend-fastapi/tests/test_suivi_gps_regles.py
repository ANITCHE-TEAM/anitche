"""Suivi GPS : règles d'accès (fonctions pures) et estimation distance/ETA.

Table rôle × lien avec la livraison × statut, sans application ni base.
Référence : `livraisons_visibles` et TRANSITIONS de Django
(apps/livraison/views.py, apps/livraison/services.py).
"""
import itertools
import json
import math

import pytest

from app.services import eta
from app.services.delivery_access import DeliveryAccess, Refusal, can_listen, can_publish

USER_ID = 10
OTHER_ID = 20
STATUSES = ["en_attente", "expediee", "en_cours", "livree", "echouee", "annulee"]
ROLES = ["client", "vendeur", "moderateur", "support", "livreur", "admin", "super_admin"]
# Lien de l'utilisateur avec la livraison.
LINKS = {
    "aucun": {"courier_id": OTHER_ID, "client_id": OTHER_ID},
    "client_de_la_commande": {"courier_id": OTHER_ID, "client_id": USER_ID},
    "livreur_assigne": {"courier_id": USER_ID, "client_id": OTHER_ID},
    "livreur_et_client": {"courier_id": USER_ID, "client_id": USER_ID},
    "sans_livreur_client": {"courier_id": None, "client_id": USER_ID},
}

# Écoute : qui voit la livraison (avant le contrôle du statut), dans l'ordre
# de livraisons_visibles. Écrit à la main, pas recalculé.
VISIBLE = {
    ("admin", link) for link in LINKS
} | {
    ("super_admin", link) for link in LINKS
} | {
    ("livreur", "livreur_assigne"),
    ("livreur", "livreur_et_client"),
} | {
    (role, link)
    for role in ("client", "vendeur", "moderateur", "support")
    for link in ("client_de_la_commande", "livreur_et_client", "sans_livreur_client")
}


def access(role, link, status, *, active=True, destination=None) -> DeliveryAccess:
    return DeliveryAccess(
        status=status, user_role=role, user_active=active, destination=destination, **LINKS[link]
    )


@pytest.mark.parametrize("role, link, status", list(itertools.product(ROLES, LINKS, STATUSES)))
def test_can_listen_table(role, link, status):
    refusal = can_listen(access(role, link, status), USER_ID)
    if (role, link) not in VISIBLE:
        assert refusal is Refusal.DELIVERY_NOT_FOUND
    elif status != "en_cours":
        assert refusal is Refusal.NOT_IN_PROGRESS
    else:
        assert refusal is None


def test_livreur_branch_comes_before_client_branch():
    """Comme Django : un livreur qui a aussi passé la commande ne la voit
    que s'il en est le livreur assigné."""
    assert can_listen(access("livreur", "client_de_la_commande", "en_cours"), USER_ID) is Refusal.DELIVERY_NOT_FOUND


@pytest.mark.parametrize("role", ROLES)
def test_inactive_or_deleted_account_never_listens(role):
    assert can_listen(access(role, "livreur_et_client", "en_cours", active=False), USER_ID) is Refusal.DELIVERY_NOT_FOUND
    assert can_listen(access(None, "livreur_et_client", "en_cours"), USER_ID) is Refusal.DELIVERY_NOT_FOUND


def test_missing_delivery():
    assert can_listen(None, USER_ID) is Refusal.DELIVERY_NOT_FOUND
    assert can_publish(None, USER_ID) is Refusal.DELIVERY_NOT_FOUND


@pytest.mark.parametrize("role, link, status", list(itertools.product(ROLES, LINKS, STATUSES)))
def test_can_publish_table(role, link, status):
    """Seul le livreur assigné publie, pendant en_cours. L'administration ne
    publie jamais."""
    refusal = can_publish(access(role, link, status), USER_ID)
    if role != "livreur":
        assert refusal is Refusal.COURIERS_ONLY
    elif link not in ("livreur_assigne", "livreur_et_client"):
        assert refusal is Refusal.NOT_ASSIGNED
    elif status != "en_cours":
        assert refusal is Refusal.NOT_IN_PROGRESS
    else:
        assert refusal is None


def test_inactive_courier_cannot_publish():
    assert can_publish(access("livreur", "livreur_assigne", "en_cours", active=False), USER_ID) is Refusal.COURIERS_ONLY


def test_refusal_codes_are_stable():
    """Codes machine lus par le frontend (errors.code[0])."""
    assert {refusal.value for refusal in Refusal} == {
        "livraison_introuvable",
        "acces_reserve_livreurs",
        "livraison_non_assignee",
        "livraison_pas_en_cours",
    }


# ------------------------------------------------------------ ETA

ESTIMATE = {"detour_factor": 1.4, "average_speed_kmh": 20.0}


def test_haversine_reference_distances():
    # Un degré de latitude : 111,19 km (rayon moyen 6 371 km).
    assert eta.haversine_km(0, 0, 1, 0) == pytest.approx(111.19, abs=0.01)
    assert eta.haversine_km(5.3, -4.0, 5.3, -4.0) == 0
    # Plateau -> Angré (Abidjan) : environ 9,1 km à vol d'oiseau.
    assert eta.haversine_km(5.3200, -4.0150, 5.397340, -3.986620) == pytest.approx(9.15, abs=0.1)


def test_estimate_with_the_client_point():
    distance, minutes = eta.estimate(5.3200, -4.0150, (5.397340, -3.986620), **ESTIMATE)
    raw = eta.haversine_km(5.3200, -4.0150, 5.397340, -3.986620) * 1.4
    assert distance == round(raw, 1) == 12.8
    assert minutes == math.ceil(raw / 20 * 60) == 39


def test_estimate_without_the_client_point_is_null():
    assert eta.estimate(5.32, -4.01, None, **ESTIMATE) == (None, None)


def test_estimate_on_arrival_is_at_least_one_minute():
    assert eta.estimate(5.3973, -3.9866, (5.3973, -3.9866), **ESTIMATE) == (0.0, 1)


POINTS = [-90, -45.5, -0.0001, 0, 5.39734, 89.9999, 90]
LONGITUDES = [-180, -179.9999, -3.98662, 0, 179.9999, 180]


@pytest.mark.parametrize("lat1, lon1", list(itertools.product(POINTS, LONGITUDES)))
def test_estimate_is_never_nan_or_infinite(lat1, lon1):
    for lat2, lon2 in itertools.product(POINTS, LONGITUDES):
        distance, minutes = eta.estimate(lat1, lon1, (lat2, lon2), **ESTIMATE)
        assert math.isfinite(distance) and distance >= 0
        assert isinstance(minutes, int) and minutes >= 1
        json.dumps({"d": distance, "m": minutes}, allow_nan=False)


def test_antipodes_do_not_break_asin():
    distance, _ = eta.estimate(0, 0, (0, 180), detour_factor=1.0, average_speed_kmh=20)
    assert distance == pytest.approx(math.pi * eta.EARTH_RADIUS_KM, abs=0.1)
