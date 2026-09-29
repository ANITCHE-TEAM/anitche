"""Module 3 : scan QR (app/routeurs/scan_qr.py, app/services/qr_decode.py).

FastAPI décode seulement (option A du rapport module 3) : il dit si le
contenu d'un QR, ou un code saisi, désigne un passeport ANITCHE, sans rien
certifier. Django certifie, compte et journalise le scan
(GET /api/passeports/verifier/<code>/) : aucune base ni aucun appel à Django
ici.

Diagnostic du rapport module 3 couvert : 1 (passeports en dur), 2 (compteur
en mémoire), 3 (domaine en dur, domaines étrangers certifiés), 4 (200
`valide: false`), 5 (aucune taille maximale, saisie recopiée), 6 et 6 bis
(extraction par regex, saisies légitimes refusées), 7 (faux certificat),
A (routes synchrones), C (résumé OpenAPI trompeur).

Les 38 cas du prototype du rapport (§ 2 e : 10 saisies acceptées, 27
contournements refusés, origine de dev) sont repris ici tels que listés,
avec FRONTEND_BASE_URL = https://anitche.com (valeur de prod par défaut).
"""
import importlib
import inspect
import logging
import re
from pathlib import Path
from types import FunctionType, MappingProxyType, ModuleType

import fakeredis
import pytest
import yaml

from app.routeurs import scan_qr as scan_qr_router
from app.services import qr_decode
from app.services.qr_decode import QrRefusal, QrRefused
from tests.fakes import make_settings

REPO = Path(__file__).resolve().parents[2]
FRONTEND = "https://anitche.com"
CODE = "PAS-2026-1A2B3C4D"
URL = f"{FRONTEND}/qr/verifier/{CODE}"
ERROR_KEYS = {"success", "status_code", "detail", "errors"}

INVALID = QrRefusal.INVALID_CODE.value
FOREIGN = QrRefusal.NOT_ANITCHE.value
NOT_PASSPORT = QrRefusal.NOT_PASSPORT_LINK.value


@pytest.fixture
def settings(request):
    """FRONTEND_BASE_URL de prod par défaut dans ce module ; autre valeur :
    @pytest.mark.parametrize("settings", [{...}], indirect=True)."""
    return make_settings(**{"frontend_base_url": FRONTEND, **getattr(request, "param", {})})


def scan(client, qr_data, **kwargs):
    return client.post("/qr/scan", json={"qr_data": qr_data}, **kwargs)


def refusal_of(response) -> str:
    assert response.status_code == 400, response.text
    body = response.json()
    assert set(body) == ERROR_KEYS and body["success"] is False and body["status_code"] == 400
    (code,) = body["errors"]["code"]
    assert set(body["errors"]) == {"code"}
    assert body["detail"] == qr_decode.REFUSAL_MESSAGES[QrRefusal(code)]
    return code


# ------------------------------------------------ 38 cas du prototype (§ 2 e)

PROTOTYPE_ACCEPTED = [
    pytest.param(CODE, id="code"),
    pytest.param(URL, id="url"),
    pytest.param("PAS 2026 1A2B3C4D", id="code avec espaces"),
    pytest.param("PAS20261A2B3C4D", id="code sans tirets"),
    pytest.param("PAS\u20132026\u20131A2B3C4D", id="tirets demi-cadratins"),
    pytest.param("pas-2026-1a2b3c4d", id="minuscules"),
    pytest.param("  PAS-2026-1A2B3C4D\n", id="espaces et saut de ligne autour"),
    pytest.param("HTTPS://ANITCHE.COM/QR/VERIFIER/PAS-2026-1A2B3C4D", id="url en majuscules"),
    pytest.param(f"{URL}/", id="slash final"),
    pytest.param(f"{URL}?utm_source=etiquette&utm_medium=qr#haut", id="parametres de suivi et fragment"),
]

PROTOTYPE_REFUSED = [
    # Diagnostic 3 : certifiés par l'ancienne maquette.
    pytest.param(f"https://evil.example/qr/verifier/{CODE}", FOREIGN, id="autre domaine"),
    pytest.param(f"https://anitche.com.evil.example/qr/verifier/{CODE}", FOREIGN, id="sous-domaine piege"),
    pytest.param(f"https://anitche.com@evil.example/qr/verifier/{CODE}", FOREIGN, id="identifiants userinfo"),
    pytest.param(f"https://\u0430nitche.com/qr/verifier/{CODE}", FOREIGN, id="homographe cyrillique"),
    pytest.param(f"javascript:alert(document.cookie)//{CODE}", FOREIGN, id="javascript"),
    pytest.param(f"data:text/html,<script>alert(1)</script>{CODE}/", FOREIGN, id="data"),
    pytest.param(f"mailto:contact@evil.example?subject={CODE}", FOREIGN, id="mailto"),
    # urlsplit lit l'hôte anitche.com, un navigateur ouvre evil.example.
    pytest.param(f"https://evil.example\\@anitche.com/qr/verifier/{CODE}", FOREIGN, id="antislash"),
    pytest.param(f"http://anitche.com/qr/verifier/{CODE}", FOREIGN, id="http au lieu de https"),
    pytest.param(f"https://anitche.com:8443/qr/verifier/{CODE}", FOREIGN, id="autre port"),
    pytest.param(f"https://anitche.com:abc/qr/verifier/{CODE}", FOREIGN, id="port non numerique"),
    pytest.param(f"https://anitche.com./qr/verifier/{CODE}", FOREIGN, id="hote avec point final"),
    pytest.param(f"https://evil.example/{CODE}#{URL}", FOREIGN, id="fragment piege"),
    # Bonne origine, autre page.
    pytest.param(f"{FRONTEND}/produits/robe-baoule", NOT_PASSPORT, id="fiche produit"),
    pytest.param(f"{URL}/suite", NOT_PASSPORT, id="segment apres le code"),
    pytest.param(f"{FRONTEND}/qr/verifier/PAS-2026-TIASSALE01", NOT_PASSPORT, id="ancien format dans url"),
    pytest.param(f"{URL}%2F..%2Fadmin", NOT_PASSPORT, id="slash encode"),
    pytest.param(f"{FRONTEND}/qr/verifier/PAS-2026-1A2B3\u04214D", NOT_PASSPORT, id="lettre cyrillique dans le code"),
    # Ni code ni URL.
    pytest.param("n'importe quoi", INVALID, id="texte quelconque"),
    pytest.param("PAS-2026-TIASSALE01", INVALID, id="ancien format"),
    pytest.param("PA\u017f-2026-1A2B3C4D", INVALID, id="s long"),
    pytest.param("PAS-2026-T\u0131ASSALE01", INVALID, id="i sans point"),
    pytest.param("PAS-\uff12\uff10\uff12\uff16-1A2B3C4D", INVALID, id="chiffres pleine chasse"),
    pytest.param(f"voir {CODE} svp", INVALID, id="texte autour du code"),
    pytest.param("PAS-<script>", INVALID, id="balise"),
    pytest.param("PAS-2026-" + "A" * 200, INVALID, id="code de 209 caracteres"),
    pytest.param(f"anitche.com/qr/verifier/{CODE}", INVALID, id="url sans schema"),
]


def test_prototype_counts_match_the_report():
    assert len(PROTOTYPE_ACCEPTED) == 10
    assert len(PROTOTYPE_REFUSED) == 27
    refusals = [case.values[1] for case in PROTOTYPE_REFUSED]
    assert (refusals.count(FOREIGN), refusals.count(NOT_PASSPORT), refusals.count(INVALID)) == (13, 5, 9)


@pytest.mark.parametrize("qr_data", PROTOTYPE_ACCEPTED)
def test_prototype_accepted_inputs(client, qr_data):
    """Diagnostic 6 bis : saisies légitimes, toutes ramenées au même code."""
    response = scan(client, qr_data)
    assert response.status_code == 200, response.text
    assert response.json() == {"code_passeport": CODE, "url_verification_publique": URL}


@pytest.mark.parametrize("qr_data, refusal", PROTOTYPE_REFUSED)
def test_prototype_bypasses_are_refused(client, qr_data, refusal):
    """Diagnostic 3, 4 et 6 : 400 avec le code machine, jamais un 200."""
    assert refusal_of(scan(client, qr_data)) == refusal


@pytest.mark.parametrize("settings", [{"frontend_base_url": "http://localhost:5173"}], indirect=True)
def test_prototype_dev_origin_is_accepted(client):
    response = scan(client, f"http://localhost:5173/qr/verifier/{CODE}")
    assert response.status_code == 200
    assert response.json()["url_verification_publique"] == f"http://localhost:5173/qr/verifier/{CODE}"


# ------------------------------------------------ décodeur : autres cas


@pytest.mark.parametrize(
    "qr_data",
    [
        *(f"PAS{dash}2026{dash}1A2B3C4D" for dash in "\u2010\u2011\u2012\u2013\u2014\u2212"),
        "PAS\u00a02026\u00a01A2B3C4D",  # espace insécable
        "PAS\u202f2026\u202f1A2B3C4D",  # espace fine insécable
        "PAS\t2026\t1A2B3C4D",
        "pas 2026-1a2b 3c4d",
        f"https://anitche.com:443/qr/verifier/{CODE}",  # port par défaut explicite : même origine
        f"https://Anitche.Com/QR/Verifier/{CODE.lower()}/",
        f"{URL}#https://evil.example/",  # fragment ignoré, l'hôte est anitche.com
    ],
)
def test_more_accepted_inputs(qr_data):
    assert qr_decode.decode(qr_data, FRONTEND) == CODE


@pytest.mark.parametrize(
    "qr_data",
    [
        "PAS-2026-1A2B3C4",  # 7 hexadécimaux
        "PAS-2026-1A2B3C4D5",  # 9
        "PAS-2026-1A2B3C4G",  # G non hexadécimal
        "PAS-226-1A2B3C4D",
        "PAS-20266-1A2B3C4D",
        "PAS-\u0662\u0660\u0662\u0666-1A2B3C4D",  # chiffres arabes-indiens
        "PAS-2026-1A2B\u200b3C4D",  # espace sans chasse
        "PAS-2026-1A2B3C4D\u0000",
        "PAS_2026_1A2B3C4D",
        "PAS.2026.1A2B3C4D",
        "- -",
        "2026-1A2B3C4D",
        f"{CODE} {CODE}",
        f"<a href='x'>{CODE}</a>",
        f"//anitche.com/qr/verifier/{CODE}",  # sans schéma
        f"/qr/verifier/{CODE}",
    ],
)
def test_more_invalid_codes(qr_data):
    with pytest.raises(QrRefused) as refused:
        qr_decode.decode(qr_data, FRONTEND)
    assert refused.value.refusal is QrRefusal.INVALID_CODE


@pytest.mark.parametrize(
    "qr_data",
    [
        f"https://anitche.ci/qr/verifier/{CODE}",  # ancien domaine : aucun QR imprimé avec
        f"https://www.anitche.com/qr/verifier/{CODE}",
        f"ftp://anitche.com/qr/verifier/{CODE}",
        f"https://anitche.com/qr/verifier/{CODE} svp",  # espace
        f"https://anitche.com/qr/verifier/\t{CODE}",
        f"https://anitche.com/qr/verifier/{CODE}\r\nX-Injecte: 1",
        f"https://anitche.com\u0000.evil.example/qr/verifier/{CODE}",
        f"https://anitche.com\u200b/qr/verifier/{CODE}",  # espace sans chasse
        f"https://\u202eanitche.com/qr/verifier/{CODE}",  # inversion du sens d'écriture
        # Antislash sans « @ » (le cas du prototype est aussi refusé par la
        # règle des identifiants) : refusé n'importe où, paramètres compris.
        f"{URL}?retour=\\evil.example",
        f"https://@anitche.com/qr/verifier/{CODE}",
        f"https://user:motdepasse@anitche.com/qr/verifier/{CODE}",
        f"https://anitche.com:99999/qr/verifier/{CODE}",
        f"https://anitche.com:+443/qr/verifier/{CODE}",
        f"https://anitche.com:443:80/qr/verifier/{CODE}",
        f"https://\uff41\uff4e\uff49\uff54\uff43\uff48\uff45.com/qr/verifier/{CODE}",  # pleine chasse
        f"https://anitche\u3002com/qr/verifier/{CODE}",  # point idéographique
        f"https://xn--nitche-2nf.com/qr/verifier/{CODE}",  # punycode
        f"https://anitche%2Ecom/qr/verifier/{CODE}",
        f"https:anitche.com/qr/verifier/{CODE}",
        f"https:///anitche.com/qr/verifier/{CODE}",
        f"https://[anitche.com/qr/verifier/{CODE}",  # ValueError d'urlsplit
        f"https://anitche.com\uff03@evil.example/{CODE}",  # ValueError d'urlsplit (NFKC)
        f"https://[::1]/qr/verifier/{CODE}",
        f"Code: {CODE}",  # « Code: » a la forme d'un schéma
        f"anitche.com:443/qr/verifier/{CODE}",  # hôte:port sans schéma : « anitche.com: » aussi
    ],
)
def test_more_foreign_or_trapped_urls(qr_data):
    with pytest.raises(QrRefused) as refused:
        qr_decode.decode(qr_data, FRONTEND)
    assert refused.value.refusal is QrRefusal.NOT_ANITCHE


@pytest.mark.parametrize(
    "qr_data",
    [
        f"{FRONTEND}/?code={CODE}",  # code ailleurs que dans le chemin
        f"{FRONTEND}/produits/robe#{CODE}",
        f"{FRONTEND}/fr/qr/verifier/{CODE}",
        f"{FRONTEND}/qr/verifier/",
        f"{FRONTEND}/qr/verifier",
        f"{FRONTEND}/qr//verifier/{CODE}",
        f"{FRONTEND}/qr/verifier/../verifier/{CODE}",
        f"{URL}//",
        f"{URL};x",
        f"{URL}%00",
        f"{FRONTEND}/qr/verifier/PAS%2D2026%2D1A2B3C4D",
        f"{FRONTEND}/qr/verifier/PAS\u20132026\u20131A2B3C4D",
        f"{FRONTEND}/qr/verifier/PA\u017f-2026-1A2B3C4D",  # « ſ ».upper() == « S »
        f"{FRONTEND}/qr/ver\u0131fier/{CODE}",  # « ı ».upper() == « I »
        f"{FRONTEND}/qr/verifier/PAS-2026-1A2B3C4",
    ],
)
def test_more_links_that_are_not_passports(qr_data):
    with pytest.raises(QrRefused) as refused:
        qr_decode.decode(qr_data, FRONTEND)
    assert refused.value.refusal is QrRefusal.NOT_PASSPORT_LINK


@pytest.mark.parametrize(
    "base_url, accepted, refused",
    [
        ("http://localhost:5173", "http://LOCALHOST:5173/qr/verifier/", "http://localhost:8000/qr/verifier/"),
        ("http://localhost:5173", "http://localhost:5173/qr/verifier/", "https://localhost:5173/qr/verifier/"),
        ("http://localhost:5173", "http://localhost:5173/qr/verifier/", "http://127.0.0.1:5173/qr/verifier/"),
        ("https://anitche.com/", "https://anitche.com/qr/verifier/", "https://anitche.com:80/qr/verifier/"),
    ],
)
def test_only_the_origin_of_frontend_base_url_is_accepted(base_url, accepted, refused):
    """Décision 4 : l'origine de FRONTEND_BASE_URL seule (schéma, hôte, port)."""
    assert qr_decode.decode(accepted + CODE, base_url) == CODE
    with pytest.raises(QrRefused) as error:
        qr_decode.decode(refused + CODE, base_url)
    assert error.value.refusal is QrRefusal.NOT_ANITCHE


def test_base_path_of_frontend_base_url_is_required():
    base_url = "https://anitche.com/boutique/"
    assert qr_decode.decode(f"https://anitche.com/boutique/qr/verifier/{CODE}", base_url) == CODE
    assert qr_decode.decode(f"https://anitche.com/BOUTIQUE/qr/verifier/{CODE}", base_url) == CODE
    for other in (URL, f"https://anitche.com/boutiques/qr/verifier/{CODE}"):
        with pytest.raises(QrRefused) as error:
            qr_decode.decode(other, base_url)
        assert error.value.refusal is QrRefusal.NOT_PASSPORT_LINK


def test_typed_code_is_checked_before_any_url_rule():
    """Un code saisi n'a pas d'origine : il est accepté quel que soit
    FRONTEND_BASE_URL."""
    for base_url in (FRONTEND, "http://localhost:5173", "https://exemple.test/app"):
        assert qr_decode.decode("pas 2026 1a2b3c4d", base_url) == CODE


# ------------------------------------------------ validation du corps


@pytest.mark.parametrize(
    "payload, message",
    [
        ({}, "Ce champ est obligatoire."),
        ({"qr_data": None}, "Chaîne de caractère invalide."),
        ({"qr_data": 20261234}, "Chaîne de caractère invalide."),
        ({"qr_data": [CODE]}, "Chaîne de caractère invalide."),
        ({"qr_data": {"code": CODE}}, "Chaîne de caractère invalide."),
        ({"qr_data": ""}, "Ce champ ne peut être vide."),
        ({"qr_data": " \n\t "}, "Ce champ ne peut être vide."),
        ({"qr_data": "x" * 513}, "Assurez-vous que ce champ comporte au plus 512\xa0caractères."),
    ],
)
def test_invalid_body_is_400_on_qr_data(client, payload, message):
    response = client.post("/qr/scan", json=payload)
    assert response.status_code == 400
    body = response.json()
    assert body["errors"] == {"qr_data": [message]}
    assert body["detail"] == f"qr_data: {message}"


def test_maximum_length_is_512_characters_after_stripping(client):
    """Décision 3 : 512 caractères au plus, espaces de début et de fin
    retirés d'abord."""
    padding = "a" * (512 - len(f"{URL}?x="))
    longest = f"{URL}?x={padding}"
    assert len(longest) == 512
    assert scan(client, longest).status_code == 200
    assert scan(client, f"   {longest}\n  ").status_code == 200
    too_long = scan(client, longest + "a")
    assert too_long.status_code == 400 and set(too_long.json()["errors"]) == {"qr_data"}


def test_huge_body_is_refused_without_being_processed(client):
    """Diagnostic 5 : 1 Mo donnait une réponse de 3 Mo. Module 4 : refusé
    dès l'en-tête Content-Length (413, MAX_REQUEST_BODY_BYTES), sans lire
    le corps ; au-dessous de la limite, 400 (test précédent)."""
    response = scan(client, "PAS-" + "A" * 1_000_000)
    assert response.status_code == 413
    assert response.json()["errors"] == {"code": ["corps_trop_volumineux"]}
    assert len(response.content) < 300


def test_unknown_fields_are_ignored(client):
    response = client.post("/qr/scan", json={"qr_data": CODE, "valide": True, "nb_scans": 99})
    assert response.json() == {"code_passeport": CODE, "url_verification_publique": URL}


# ------------------------------------------------ aucune recopie de la saisie

MARKER = "MarqueurSaisie7Q"

ECHO_CASES = [
    f"https://evil.example/{MARKER}",
    f"https://anitche.com/{MARKER}",
    f"{URL}?ref={MARKER}#{MARKER}",  # accepté : seule la forme canonique revient
    f"<img src=x onerror=alert(1)>{MARKER}",
    f"PAS-{MARKER}",
    f"\x00\x01\x1b[31m{MARKER}",
    f"{MARKER}:{MARKER}",
    MARKER * 40,  # trop long
]


@pytest.mark.parametrize("qr_data", ECHO_CASES)
def test_input_is_never_echoed_in_response_or_logs(client, caplog, qr_data):
    """Diagnostic 5 : ni dans la réponse (succès comme refus), ni dans les
    journaux, à aucun niveau."""
    caplog.set_level(logging.DEBUG)
    response = scan(client, qr_data)
    assert response.status_code in (200, 400)
    assert MARKER.lower() not in response.text.lower()
    assert MARKER.lower() not in caplog.text.lower()
    assert "POST /qr/scan" in caplog.text  # journal d'accès : méthode et chemin seulement


def test_refusal_messages_never_contain_input():
    for message in qr_decode.REFUSAL_MESSAGES.values():
        assert "{" not in message and "%" not in message


# ------------------------------------------------ réponse


@pytest.mark.parametrize(
    "settings, expected",
    [
        ({"frontend_base_url": "https://anitche.com"}, f"https://anitche.com/qr/verifier/{CODE}"),
        # Même valeur que le test Django test_url_de_verification_calculee_depuis_le_reglage.
        ({"frontend_base_url": "https://exemple.test/"}, f"https://exemple.test/qr/verifier/{CODE}"),
        ({"frontend_base_url": "https://anitche.com/boutique"}, f"https://anitche.com/boutique/qr/verifier/{CODE}"),
        ({"frontend_base_url": "http://localhost:5173"}, f"http://localhost:5173/qr/verifier/{CODE}"),
    ],
    indirect=["settings"],
)
def test_response_is_the_code_and_the_url_built_from_frontend_base_url(client, expected):
    """Décision 7 : exactement code_passeport et url_verification_publique,
    construite comme Django (FRONTEND_BASE_URL sans « / » final +
    /qr/verifier/ + code), jamais depuis la saisie."""
    response = scan(client, "pas 2026 1a2b3c4d")
    assert response.status_code == 200
    assert response.json() == {"code_passeport": CODE, "url_verification_publique": expected}


def test_verification_url_is_built_like_django():
    for base_url in ("https://anitche.com", "https://anitche.com/", "https://anitche.com//"):
        assert qr_decode.verification_url(CODE, base_url) == URL


def test_response_is_not_a_certificate(client):
    """Diagnostic 1, 4 et 7 : aucun champ de certificat, et un code qui
    n'existe nulle part se décode comme un autre (aucun oracle
    d'existence) : c'est Django qui certifie."""
    known = scan(client, CODE).json()
    unknown = scan(client, "PAS-1999-FFFFFFFF").json()
    assert set(known) == set(unknown) == {"code_passeport", "url_verification_publique"}
    assert unknown["code_passeport"] == "PAS-1999-FFFFFFFF"


def test_old_hard_coded_passports_are_just_invalid_codes(client):
    """Diagnostic 1 : les 3 passeports inventés n'ont pas le format de
    Django."""
    for old in ("PAS-2026-TIASSALE01", "PAS-2026-BASSAM02", "PAS-2026-MASQUE03"):
        assert refusal_of(scan(client, old)) == INVALID


# ------------------------------------------------ isolation et état


def test_no_database_no_django_no_cache(client, db, django, redis_server):
    """Option A : ni requête SQL, ni appel à Django ; dans Redis, seul le
    compteur de la limite de débit."""
    for qr_data in (CODE, URL, "n'importe quoi", "https://evil.example/", f"{FRONTEND}/produits/x"):
        scan(client, qr_data)
    assert db.queries == []
    assert django.requests == []
    keys = fakeredis.FakeRedis(server=redis_server, decode_responses=True).keys("*")
    assert keys and all(key.startswith("fastapi:rl:qr_scan:ip:") for key in keys)


def test_repeated_scans_give_identical_responses(client):
    """Diagnostic 2 : plus de compteur (l'ancienne maquette ajoutait 1 à
    chaque appel, par processus)."""
    responses = [scan(client, CODE) for _ in range(3)]
    assert len({response.text for response in responses}) == 1


@pytest.mark.parametrize("module", [qr_decode, scan_qr_router], ids=["qr_decode", "scan_qr"])
def test_modules_hold_no_mutable_state(module):
    """Diagnostic 2 : aucun dictionnaire, liste ou ensemble modifiable au
    niveau du module (l'ancien registre était un dict modifié à chaque
    scan)."""
    for name, value in vars(module).items():
        if name.startswith("__") or isinstance(value, (ModuleType, FunctionType, type)):
            continue
        assert not isinstance(value, (dict, list, set, bytearray)), name
        if isinstance(value, MappingProxyType):
            with pytest.raises(TypeError):
                value["x"] = 1  # type: ignore[index]


def test_old_service_is_removed():
    """Diagnostic 1 et 2 : registre en dur et compteur supprimés."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.services.qr_service")


def test_route_is_asynchronous():
    """Constat A : aucune E/S, pas besoin du pool de threads."""
    assert inspect.iscoroutinefunction(scan_qr_router.scanner_qr)


# ------------------------------------------------ routes et OpenAPI


def test_get_passport_route_is_removed(client):
    """Décision 5 : GET /qr/passeport/{code} supprimée (consultation =
    Django, GET /api/passeports/verifier/{code}/)."""
    response = client.get(f"/qr/passeport/{CODE}")
    assert response.status_code == 404
    assert response.json() == {"success": False, "status_code": 404, "detail": "Ressource introuvable.", "errors": {}}


def test_get_on_scan_is_405(client):
    assert client.get("/qr/scan").status_code == 405


def test_root_still_lists_the_scan_route(client):
    assert client.get("/").json()["endpoints"]["qr_scan"] == "/qr/scan"


def test_openapi_documents_the_decoding_contract(client):
    schema = client.get("/openapi.json").json()
    qr_paths = {path: set(operations) for path, operations in schema["paths"].items() if path.startswith("/qr")}
    assert qr_paths == {"/qr/scan": {"post"}}

    operation = schema["paths"]["/qr/scan"]["post"]
    assert "certifier" in operation["summary"] and "authentifiée" not in operation["summary"]
    bad_request = operation["responses"]["400"]
    assert bad_request["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/Error"}
    for code in (INVALID, FOREIGN, NOT_PASSPORT):
        assert code in bad_request["description"]
    assert "422" not in operation["responses"]

    components = schema["components"]["schemas"]
    assert set(components["ReponseScanQR"]["properties"]) == {"code_passeport", "url_verification_publique"}
    assert components["DemandeScanQR"]["properties"]["qr_data"]["maxLength"] == 512
    assert "ReponseScanPasseport" not in components


# ------------------------------------------------ limite de débit


@pytest.mark.parametrize(
    "settings", [{"rate_limits": {"qr_scan": "2/minute"}, "trusted_proxy_count": 1}], indirect=True
)
def test_qr_scan_limit_per_ip_with_retry_after(client, redis_server):
    """Décision 6 : limite `qr_scan` par IP ; un refus compte aussi (sinon
    on essaierait des contenus sans limite)."""
    first_ip = {"X-Forwarded-For": "203.0.113.10"}
    assert scan(client, CODE, headers=first_ip).status_code == 200
    assert scan(client, "n'importe quoi", headers=first_ip).status_code == 400
    limited = scan(client, CODE, headers=first_ip)
    assert limited.status_code == 429
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    body = limited.json()
    assert set(body) == ERROR_KEYS and body["status_code"] == 429 and body["errors"] == {}

    assert scan(client, CODE, headers={"X-Forwarded-For": "203.0.113.11"}).status_code == 200
    keys = sorted(fakeredis.FakeRedis(server=redis_server, decode_responses=True).keys("fastapi:rl:*"))
    assert [key.rsplit(":", 1)[0] for key in keys] == [
        "fastapi:rl:qr_scan:ip:203.0.113.10",
        "fastapi:rl:qr_scan:ip:203.0.113.11",
    ]


def test_default_qr_scan_limit_is_unchanged(settings):
    assert settings.rate_limits["qr_scan"] == "600/hour"


# ------------------------------------------------ parité avec Django


def test_verification_path_is_the_django_one():
    """Même chemin que CHEMIN_VERIFICATION_PUBLIQUE (Django) : c'est l'URL
    imprimée dans les QR."""
    source = (REPO / "backend-django" / "apps" / "passeport_qr" / "models.py").read_text(encoding="utf-8")
    (django_path,) = re.findall(r'^CHEMIN_VERIFICATION_PUBLIQUE = "([^"]+)"$', source, re.MULTILINE)
    assert django_path == qr_decode.VERIFICATION_PATH + "{code}"


def test_code_format_is_the_django_one():
    """Format de generer_code_passeport (Django) :
    f"PAS-{année}-{uuid4().hex[:8].upper()}"."""
    source = (REPO / "backend-django" / "apps" / "passeport_qr" / "models.py").read_text(encoding="utf-8")
    assert 'f"PAS-{timezone.now().year}-{uuid.uuid4().hex[:8].upper()}"' in source
    assert qr_decode.CODE_PATTERN.fullmatch("PAS-2026-0123ABCD")
    assert not qr_decode.CODE_PATTERN.fullmatch("PAS-2026-0123abcd")


def test_django_and_fastapi_read_the_same_frontend_base_url():
    """Décision 4 : l'origine acceptée est celle que Django met dans les QR.
    Prod : même variable pour les deux services. Dev : même valeur."""
    prod = yaml.safe_load((REPO / "infra" / "docker-compose.prod.yml").read_text(encoding="utf-8"))["services"]
    assert (
        prod["backend-django"]["environment"]["FRONTEND_BASE_URL"]
        == prod["backend-fastapi"]["environment"]["FRONTEND_BASE_URL"]
        == "${FRONTEND_BASE_URL:-https://anitche.com}"
    )
    dev = yaml.safe_load((REPO / "infra" / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    django_base = (REPO / "backend-django" / "config" / "settings" / "base.py").read_text(encoding="utf-8")
    assert "FRONTEND_BASE_URL = config('FRONTEND_BASE_URL', default='http://localhost:5173')" in django_base
    assert dev["backend-fastapi"]["environment"]["FRONTEND_BASE_URL"] == "http://localhost:5173"
