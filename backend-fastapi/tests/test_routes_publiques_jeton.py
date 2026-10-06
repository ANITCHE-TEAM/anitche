"""Routes publiques et jeton : /recherche/produits, /recherche/suggestions et
/qr/scan ignorent l'en-tête Authorization.

Un jeton expiré, révoqué ou malformé donne la même réponse qu'une requête
sans en-tête, et Django n'est jamais appelé. Un jeton invalide ne doit
jamais faire perdre le catalogue (ni le scan d'un QR) à un client dont le
jeton vient d'expirer. Les routes authentifiées (IA, suivi GPS) restent en
401 : voir test_core_auth.py.
"""
import pytest

from tests.fakes import AUTH_HEADERS, TOKEN, product_row

CODE = "PAS-2026-1A2B3C4D"

ROUTES = {
    "produits": lambda client, headers: client.get("/recherche/produits?recherche=karite", headers=headers),
    "suggestions": lambda client, headers: client.get("/recherche/suggestions?recherche=kar", headers=headers),
    "scan": lambda client, headers: client.post("/qr/scan", json={"qr_data": CODE}, headers=headers),
}

TOKENS = {
    "valide": AUTH_HEADERS,
    "malforme_espaces": {"Authorization": "Bearer jeton avec des espaces"},
    "malforme_caracteres": {"Authorization": "Bearer <script>alert(1)</script>"},
    "malforme_trop_long": {"Authorization": "Bearer " + "a" * 5000},
    "bearer_vide": {"Authorization": "Bearer "},
    "autre_schema": {"Authorization": "Basic dXNlcjpwYXNz"},
    "sans_schema": {"Authorization": TOKEN},
}


@pytest.fixture(autouse=True)
def catalogue(db):
    db.search_rows = [product_row()]
    db.suggestion_rows = [{"type": "produit", "texte": "Karité", "id": 41, "slug": "beurre-de-karite-pur-3d85cf"}]


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("etat", ["expire", "revoque", *TOKENS])
def test_jeton_ignore_reponse_identique_zero_appel_django(client, django, route, etat):
    appeler = ROUTES[route]
    reference = appeler(client, None)
    assert reference.status_code == 200, reference.text

    if etat == "expire":
        django.status_code, django.json = 401, {"detail": "Le jeton est expiré."}
        headers = AUTH_HEADERS
    elif etat == "revoque":
        django.users_by_token[TOKEN] = None
        headers = AUTH_HEADERS
    else:
        headers = TOKENS[etat]

    reponse = appeler(client, headers)

    assert reponse.status_code == 200
    assert reponse.json() == reference.json()
    assert "www-authenticate" not in reponse.headers
    assert django.requests == []


@pytest.mark.parametrize("route", ROUTES)
def test_jeton_ignore_aussi_quand_django_est_en_panne(client, django, route):
    """Django indisponible (503 sur les routes authentifiées) : sans effet ici."""
    django.error = RuntimeError("Django injoignable")
    reponse = ROUTES[route](client, AUTH_HEADERS)
    assert reponse.status_code == 200
    assert django.requests == []
