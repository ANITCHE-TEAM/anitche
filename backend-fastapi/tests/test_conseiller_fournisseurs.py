"""Module 4 : fournisseurs du conseiller IA (docs/MODULE_IA.md).

Registre, réglages, validation de la sortie HORS du fournisseur (avec de
faux fournisseurs qui inventent des identifiants, renvoient un JSON
invalide, dépassent le délai ou tombent en panne), garde-fou de budget,
lexique et fournisseur simulé.
"""
import asyncio
import logging
import sys
import time
import types
from datetime import UTC, datetime

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.resources import Resources
from app.core.settings import DEFAULT_RATE_LIMITS, Settings
from app.main import create_app
from app.services.conseiller import lexique, service
from app.services.conseiller import fournisseurs as registre
from app.services.conseiller.fournisseurs.base import (
    Candidat,
    DemandeFournisseur,
    ErreurFournisseurIA,
    FournisseurIA,
)
from app.services.conseiller.fournisseurs.simule import FournisseurSimule
from tests.fakes import AUTH_HEADERS, make_settings, product_row

CONSEIL = "/ia/conseil"
ROWS = [product_row(id=33, nom="Robe en bazin brodé", categorie_nom="Mode"), product_row(id=41), product_row(id=42)]


class Faux(FournisseurIA):
    """Faux fournisseur « ia » (payant par défaut, comme un vrai)."""

    code = "faux"

    def __init__(self, sortie=None, *, erreur=None, delai=0.0, payant=True):
        self.sortie = sortie
        self.erreur = erreur
        self.delai = delai
        self.payant = payant
        self.demandes = []
        self.ferme = False

    async def conseiller(self, demande):
        self.demandes.append(demande)
        if self.delai:
            await asyncio.sleep(self.delai)
        if self.erreur is not None:
            raise self.erreur
        return self.sortie(demande) if callable(self.sortie) else self.sortie

    async def fermer(self):
        self.ferme = True


def valide(*identifiants, message="Deux idées pour vous.") -> dict:
    return {"produits": [{"id": i, "justification": f"Choix {i}."} for i in identifiants],
            "message": message, "conseils": ["Un conseil."]}


@pytest.fixture
def make_client(django, redis, db):
    clients = []
    db.search_rows = list(ROWS)

    def factory(provider=None, **overrides):
        settings = make_settings(**overrides)
        http = httpx.AsyncClient(base_url=settings.django_api_base_url, transport=httpx.MockTransport(django))
        client = TestClient(create_app(settings, resources=Resources(http=http, db=db, redis=redis), ai_provider=provider))
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


def ask(client, texte="Une robe en bazin pour un mariage"):
    response = client.post(CONSEIL, json={"messages": [{"role": "user", "contenu": texte}]}, headers=AUTH_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def ids(body) -> list[int]:
    return [product["id"] for product in body["produits_suggeres"]]


# ------------------------------------------------------------ registre et réglages


def test_simulated_provider_is_the_default():
    settings = Settings(_env_file=None)
    assert settings.ai_enabled is True and settings.ai_provider == "simule"
    assert settings.ai_timeout_seconds == 10.0 and settings.ai_daily_call_limit == 1000
    assert settings.max_request_body_bytes == 131_072
    assert DEFAULT_RATE_LIMITS["ai_advice"] == "20/hour" and DEFAULT_RATE_LIMITS["ai_recommendations"] == "120/hour"
    assert isinstance(create_app(make_settings()).state.conseiller_ia, FournisseurSimule)
    assert registre.FOURNISSEURS == {"simule": "app.services.conseiller.fournisseurs.simule"}


def test_environment_variables_are_read(monkeypatch):
    for name, value in {"AI_ENABLED": "false", "AI_PROVIDER": "simule", "AI_TIMEOUT_SECONDS": "2.5",
                        "AI_DAILY_CALL_LIMIT": "50", "MAX_REQUEST_BODY_BYTES": "2048"}.items():
        monkeypatch.setenv(name, value)
    settings = Settings(_env_file=None)
    assert (settings.ai_enabled, settings.ai_timeout_seconds, settings.ai_daily_call_limit,
            settings.max_request_body_bytes) == (False, 2.5, 50, 2048)


@pytest.mark.parametrize("field, value", [
    ("ai_timeout_seconds", 0), ("ai_timeout_seconds", 31), ("ai_daily_call_limit", -1),
    ("ai_daily_call_limit", 1_000_001), ("ai_provider", ""), ("max_request_body_bytes", 1023),
    ("max_request_body_bytes", 1_048_577),
])
def test_settings_bounds(field, value):
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


@pytest.mark.parametrize("code", ["gemini", "openai", "Simule", "../simule", "simule.py", "os"])
def test_unknown_provider_prevents_startup(code):
    with pytest.raises(registre.FournisseurIAInconnu) as error:
        create_app(make_settings(ai_provider=code))
    assert str(error.value) == f"AI_PROVIDER inconnu : « {code} » (valeurs possibles : simule)"


@pytest.fixture
def module_fournisseur(monkeypatch):
    """Module de fournisseur factice inscrit dans le registre."""
    module = types.ModuleType("tests_fournisseur_factice")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(registre.FOURNISSEURS, "faux", module.__name__)
    return module


def test_registered_provider_is_created_and_closed_at_shutdown(module_fournisseur, django, redis, db):
    provider = Faux(valide())
    module_fournisseur.creer = lambda settings: provider
    settings = make_settings(ai_provider="faux")
    http = httpx.AsyncClient(base_url=settings.django_api_base_url, transport=httpx.MockTransport(django))
    app = create_app(settings, resources=Resources(http=http, db=db, redis=redis))
    assert app.state.conseiller_ia is provider
    with TestClient(app):
        assert provider.ferme is False
    assert provider.ferme is True


def test_injected_provider_is_not_closed_by_the_app(make_client):
    provider = Faux(valide())
    client = make_client(provider)
    client.__exit__(None, None, None)
    assert provider.ferme is False


@pytest.mark.parametrize("created", [object(), FournisseurSimule()])
def test_registry_checks_what_the_module_creates(module_fournisseur, created):
    module_fournisseur.creer = lambda settings: created
    with pytest.raises(registre.FournisseurIAInconnu, match="doit renvoyer un FournisseurIA de code « faux »"):
        create_app(make_settings(ai_provider="faux"))


def test_interface_is_abstract_and_safe_by_default():
    class Incomplet(FournisseurIA):
        code = "incomplet"

    with pytest.raises(TypeError):
        Incomplet()
    assert FournisseurIA.payant is True and FournisseurIA.source == "ia"
    assert (FournisseurSimule.payant, FournisseurSimule.source, FournisseurSimule.code) == (False, "regles", "simule")


# ------------------------------------------------------------ validation de la sortie (hors fournisseur)


def test_valid_provider_output_is_used_with_source_ia(make_client):
    body = ask(make_client(Faux(valide(41, 33))))
    assert ids(body) == [41, 33] and body["source"] == "ia"
    assert body["reponse"] == "Deux idées pour vous." and body["conseils_style"] == ["Un conseil."]
    assert [p["justification"] for p in body["produits_suggeres"]] == ["Choix 41.", "Choix 33."]


def test_json_text_output_is_accepted(make_client):
    body = ask(make_client(Faux('{"produits": [{"id": 42, "justification": "Oui."}], "message": "Voici."}')))
    assert ids(body) == [42] and body["source"] == "ia" and body["conseils_style"] == []


def test_invented_ids_are_rejected_and_message_replaced(make_client):
    body = ask(make_client(Faux(valide(999, 41, 1, message="La robe 999 est parfaite."))))
    assert ids(body) == [41] and body["source"] == "ia"
    assert body["reponse"] == "Voici 1\xa0article sélectionné pour vous."


def test_only_invented_ids_give_an_empty_list(make_client):
    body = ask(make_client(Faux(valide(1, 2, 3))))
    assert body["produits_suggeres"] == []
    assert body["reponse"] == "Aucun article en stock pour votre demande. Élargissez le budget ou changez de catégorie."


def test_duplicates_are_removed_and_count_is_capped(make_client, db):
    db.search_rows = [product_row(id=i) for i in range(50, 60)]
    body = ask(make_client(Faux(valide(50, 50, 51, 52, 51, 53, 54, 55))))
    assert ids(body) == [50, 51, 52, 53]


def test_prices_and_names_always_come_from_the_catalogue(make_client):
    sortie = {"produits": [{"id": 41, "justification": "Oui.", "prix_min": 1, "nom": "Autre"}], "message": "Voici."}
    product = ask(make_client(Faux(sortie)))["produits_suggeres"][0]
    assert product["prix_min"] == 3500.0 and product["nom"] == "Beurre de karité pur"


def test_texts_are_cleaned_and_truncated(make_client):
    sortie = {
        "produits": [{"id": 41, "justification": "Très‮doux\x00 " + "x" * 400}, {"id": 42, "justification": " \n "}],
        "message": "Bonjour\x07  à\n\nvous",
        "conseils": ["a" * 250, "", "Deux", "Trois", "Quatre"],
    }
    body = ask(make_client(Faux(sortie)))
    justification = body["produits_suggeres"][0]["justification"]
    assert justification.startswith("Très doux x") and len(justification) == 300
    assert body["produits_suggeres"][1]["justification"] == "Correspond à votre demande."
    assert body["reponse"] == "Bonjour à vous"
    assert body["conseils_style"] == ["a" * 200, "Deux", "Trois"]


@pytest.mark.parametrize("message", [
    "Achetez sur https://exemple.test/promo",
    "Voir www.exemple.test",
    "HTTP://EXEMPLE.TEST",
])
def test_links_are_removed_and_message_replaced(make_client, message):
    sortie = {"produits": [{"id": 41, "justification": "Voir https://exemple.test/x ici"}], "message": message}
    body = ask(make_client(Faux(sortie)))
    assert body["reponse"] == "Voici 1\xa0article sélectionné pour vous."
    assert body["produits_suggeres"][0]["justification"] == "Voir ici"


@pytest.mark.parametrize("sortie", [
    "pas du JSON",
    '{"produits": "tout"}',
    "[]",
    {"message": "Sans produits"},
    {"produits": [{"id": "41"}]},
    {"produits": [{"id": 41.0}]},
    {"produits": [{"id": True}]},
    {"produits": [{"id": -41}]},
    {"produits": [{"id": 41, "justification": "x" * 501}]},
    {"produits": [{"id": 41}], "message": "x" * 2001},
    {"produits": [{"id": 41}] * 51},
    {"produits": [{"id": 41}], "conseils": ["a"] * 6},
    ["liste"],
    None,
    42,
])
def test_invalid_output_falls_back_to_rules(make_client, caplog, sortie):
    provider = Faux(sortie)
    with caplog.at_level(logging.WARNING, logger="anitche.fastapi.conseiller"):
        body = ask(make_client(provider))
    assert body["source"] == "regles" and ids(body) == [33]  # le simulé : « bazin » dans le nom
    assert len(provider.demandes) == 1
    assert "cause=sortie_invalide" in caplog.text


def test_timeout_falls_back_quickly(make_client, caplog):
    provider = Faux(valide(41), delai=5)
    client = make_client(provider, ai_timeout_seconds=0.05)
    started = time.perf_counter()
    with caplog.at_level(logging.WARNING, logger="anitche.fastapi.conseiller"):
        body = ask(client)
    assert time.perf_counter() - started < 2
    assert body["source"] == "regles" and "cause=delai" in caplog.text


def test_provider_error_falls_back_without_logging_the_request(make_client, caplog):
    provider = Faux(erreur=ErreurFournisseurIA("HTTPStatusError"))
    with caplog.at_level(logging.DEBUG):
        body = ask(make_client(provider), "Robe en bazin, écrivez-moi à awa@example.com")
    assert body["source"] == "regles"
    assert "cause=erreur" in caplog.text and "fournisseur=faux" in caplog.text
    assert "bazin" not in caplog.text and "example.com" not in caplog.text and "[masqué]" not in caplog.text


def test_unexpected_adapter_bug_falls_back(make_client, caplog):
    with caplog.at_level(logging.WARNING, logger="anitche.fastapi.conseiller"):
        body = ask(make_client(Faux(erreur=KeyError("choices"))))
    assert body["source"] == "regles" and "cause=exception" in caplog.text


def test_simulated_provider_failure_is_a_bug(monkeypatch, django, redis, db):
    async def casse(self, demande):
        raise RuntimeError("bogue")

    monkeypatch.setattr(FournisseurSimule, "conseiller", casse)
    settings = make_settings()
    http = httpx.AsyncClient(base_url=settings.django_api_base_url, transport=httpx.MockTransport(django))
    app = create_app(settings, resources=Resources(http=http, db=db, redis=redis))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(CONSEIL, json={"messages": [{"role": "user", "contenu": "Bonjour"}]},
                               headers=AUTH_HEADERS)
    assert response.status_code == 500 and response.json()["errors"] == {}


# ------------------------------------------------------------ garde-fou de budget


def budget_key() -> str:
    return "fastapi:ia:appels:" + datetime.now(UTC).strftime("%Y-%m-%d")


def test_daily_budget_guard(make_client, redis_server, caplog):
    provider = Faux(valide(41))
    client = make_client(provider, ai_daily_call_limit=2)
    with caplog.at_level(logging.WARNING, logger="anitche.fastapi.conseiller"):
        sources = [ask(client)["source"] for _ in range(3)]
    assert sources == ["ia", "ia", "regles"]
    assert len(provider.demandes) == 2  # le 3e appel n'a pas été envoyé
    assert "cause=budget" in caplog.text
    sync = fakeredis.FakeRedis(server=redis_server, decode_responses=True)
    assert sync.get(budget_key()) == "3" and 0 < sync.ttl(budget_key()) <= 2 * 86400


def test_zero_budget_means_no_paid_call(make_client):
    provider = Faux(valide(41))
    assert ask(make_client(provider, ai_daily_call_limit=0))["source"] == "regles"
    assert provider.demandes == []


def test_free_provider_is_not_counted(make_client, redis_server):
    ask(make_client(Faux(valide(41), payant=False), ai_daily_call_limit=0))
    assert fakeredis.FakeRedis(server=redis_server).keys("fastapi:ia:*") == []


def test_simulated_provider_is_never_counted(make_client, redis_server):
    ask(make_client(ai_daily_call_limit=0))
    assert fakeredis.FakeRedis(server=redis_server).keys("fastapi:ia:*") == []


def test_budget_guard_fails_closed_when_redis_is_down():
    class RedisEnPanne:
        def pipeline(self, transaction=True):
            raise ConnectionError("Redis injoignable")

    assert asyncio.run(service.reserver_appel(RedisEnPanne(), 1000)) is False


# ------------------------------------------------------------ textes du service


@pytest.mark.parametrize("texte, attendu", [
    ("écrivez à awa.kone+shop@mail.example.ci", "écrivez à [masqué]"),
    ("0707070707", "[masqué]"),
    ("07 07 07 07 07", "[masqué]"),
    ("07.07.07.07.07", "[masqué]"),
    ("+225 07-07-07-07-07", "[masqué]"),
    ("budget 30000 ou 30 000 FCFA, en 2026", "budget 30000 ou 30 000 FCFA, en 2026"),
    ("taille 38 à 42", "taille 38 à 42"),
])
def test_personal_data_masking(texte, attendu):
    assert service.masquer(texte) == attendu


def test_budget_formatting():
    assert lexique.formater_fcfa(1500) == "1\xa0500\xa0FCFA"
    assert lexique.formater_fcfa(9_999_999_999) == "9\xa0999\xa0999\xa0999\xa0FCFA"


# ------------------------------------------------------------ lexique


@pytest.mark.parametrize("texte, themes", [
    ("Une tenue pour une cérémonie", ["ceremonie"]),
    ("une ceremonie", ["ceremonie"]),
    ("MARIAGES et fêtes", ["ceremonie"]),
    ("J'ai besoin d'un conseil", []),  # be-soin
    ("Un chapeau pour mon fils", []),  # cha-peau
    ("Une anecdote amusante", []),  # anec-dot-e
    ("Des pommes", []),
    ("Un mariage, et du karité pour la peau", ["ceremonie", "beaute"]),
    ("Pour la peau, puis un mariage", ["beaute", "ceremonie"]),
    ("téléphone, cadeau et bureau", ["electronique", "maison"]),  # 2 thèmes au plus
])
def test_theme_detection_uses_whole_words(texte, themes):
    contexte = lexique.analyser((("user", texte),))
    assert [theme.code for theme in contexte.themes] == themes


def test_occasion_and_style_are_read():
    assert [t.code for t in lexique.analyser((("user", "Bonjour"),), occasion="cadeau").themes] == ["maison"]
    assert [t.code for t in lexique.analyser((("user", "Bonjour"),), style="bureau").themes] == ["bureau"]


def test_assistant_messages_and_old_messages_are_ignored():
    historique = (("user", "mariage"), ("user", "a"), ("user", "b"), ("assistant", "karité"), ("user", "c"))
    assert lexique.analyser(historique).themes == ()
    historique = (("user", "mariage"), ("assistant", "karité"), ("user", "et moins cher ?"))
    assert [t.code for t in lexique.analyser(historique).themes] == ["ceremonie"]


def test_search_terms_cited_first_then_theme_terms_capped():
    contexte = lexique.analyser((("user", "Une marmite pour un mariage"),))
    assert contexte.termes == ("marmite", "bazin", "pagne", "kita")  # « boubou » : au-delà de 4
    # « écouteurs » est cité ET déclenche l'électronique, avant le mariage.
    contexte = lexique.analyser((("user", "Une marmite et des écouteurs pour un mariage"),))
    assert contexte.termes == ("marmite", "écouteurs", "smartphone", "batterie")
    assert lexique.analyser((("user", "Bonjour"),)).termes == ()


def test_normalisation():
    assert lexique.normaliser("  Cérémonie, ÉCOUTEURS & Karité ! ") == "ceremonie ecouteurs karite"
    assert lexique.formes("Pagnes bijoux dot wax") == ["pagne", "bijou", "dot", "wax"]


# ------------------------------------------------------------ fournisseur simulé


def demande(texte="mariage", candidats=None, budget=None, type_="conseil", max_produits=4) -> DemandeFournisseur:
    candidats = candidats if candidats is not None else (
        Candidat(10, "Smartphone Tecno", "Électronique", "Adjamé Tech", 62000),
        Candidat(11, "Robe en bazin brodé", "Mode", "Pagnes & Style", 22000),
        Candidat(12, "Sac en pagne tissé", "Mode", "Pagnes & Style", 9500),
    )
    historique = (("user", texte),) if type_ == "conseil" else ()
    return DemandeFournisseur(type=type_, historique=historique, occasion=None, style=None, budget_max=budget,
                              categories=(), candidats=tuple(candidats), max_produits=max_produits)


def simuler(d: DemandeFournisseur) -> dict:
    return asyncio.run(FournisseurSimule().conseiller(d))


def test_simulated_is_deterministic_and_only_relevant():
    first, second = simuler(demande()), simuler(demande())
    assert first == second
    assert [p["id"] for p in first["produits"]] == [11, 12]  # le smartphone n'est pas pertinent


def test_simulated_falls_back_to_catalogue_when_nothing_relevant():
    sortie = simuler(demande("Bonjour"))
    assert [p["id"] for p in sortie["produits"]] == [10, 11, 12]
    assert sortie["message"] == "Voici 3\xa0articles du catalogue."
    sortie = simuler(demande("téléphone", candidats=[Candidat(11, "Robe en bazin brodé", "Mode", "Pagnes & Style", 22000)]))
    assert sortie["message"] == "Rien de précis pour l'électronique ; voici 1\xa0article du catalogue."


def test_simulated_uses_the_budget_to_break_ties():
    candidats = [Candidat(20, "Pagne A", "Mode", "B", 5000), Candidat(21, "Pagne B", "Mode", "B", 16000)]
    assert [p["id"] for p in simuler(demande(candidats=candidats, budget=30000))["produits"]] == [21, 20]
    assert [p["id"] for p in simuler(demande(candidats=candidats))["produits"]] == [20, 21]


def test_simulated_respects_max_products_and_empty_candidates():
    assert len(simuler(demande("Bonjour", max_produits=2))["produits"]) == 2
    sortie = simuler(demande(candidats=[], budget=5000))
    assert sortie["produits"] == [] and sortie["message"].startswith("Aucun article en stock pour une cérémonie")


def test_simulated_recommendations_have_no_advice():
    sortie = simuler(demande(type_="recommandations", max_produits=8))
    assert sortie["conseils"] == [] and len(sortie["produits"]) == 3


def test_simulated_output_passes_the_service_validation():
    d = demande(budget=30000)
    validee = service.valider_sortie(simuler(d), {c.id for c in d.candidats}, d.max_produits)
    assert [identifiant for identifiant, _ in validee.produits] == [11, 12] and validee.message


def test_simulated_is_not_paid_and_needs_no_network(monkeypatch):
    def interdit(*args, **kwargs):
        raise AssertionError("aucun appel réseau")

    monkeypatch.setattr(httpx.AsyncClient, "send", interdit)
    assert simuler(demande())["produits"]
