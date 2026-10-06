"""Contrat OpenAPI versionné (backend-fastapi/openapi.json).

Le fichier est la référence du frontend pour `/fast` : `/openapi.json` est
fermé en production. Il se régénère avec `python scripts/exporter_openapi.py`.
"""
import importlib.util
import json
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def exporteur():
    spec = importlib.util.spec_from_file_location("exporter_openapi", RACINE / "scripts" / "exporter_openapi.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fichier_versionne_a_jour(exporteur):
    """Échoue dès qu'une route, un modèle ou une description change sans
    que le fichier soit régénéré."""
    assert exporteur.FICHIER.exists(), "openapi.json absent : python scripts/exporter_openapi.py"
    assert exporteur.lire_fichier() == exporteur.generer_texte(), (
        "openapi.json n'est plus à jour : python scripts/exporter_openapi.py (depuis backend-fastapi)"
    )


def test_schema_independant_de_l_environnement(exporteur, monkeypatch):
    """Ni variable d'environnement ni .env ne change le fichier : il serait
    sinon différent d'un poste à l'autre."""
    reference = exporteur.generer_texte()
    for nom, valeur in {
        "ROOT_PATH": "/fast",
        "APP_VERSION": "9.9.9",
        "APP_NAME": "Autre",
        "ENVIRONMENT": "prod",
        "AI_PROVIDER": "inconnu",
        "DEBUG": "true",
    }.items():
        monkeypatch.setenv(nom, valeur)
    assert exporteur.generer_texte() == reference


def test_schema_sans_servers_et_chemins_sans_prefixe(exporteur):
    """Les chemins du fichier n'ont pas le préfixe nginx `/fast` : c'est la
    base de l'URL du client, pas le contrat."""
    schema = json.loads(exporteur.generer_texte())
    assert "servers" not in schema
    assert all(not chemin.startswith("/fast") for chemin in schema["paths"])


def test_check_echoue_en_donnant_la_commande(exporteur, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(exporteur, "FICHIER", tmp_path / "openapi.json")
    monkeypatch.setattr("sys.argv", ["exporter_openapi.py", "--check"])
    assert exporteur.main() == 1
    assert "python scripts/exporter_openapi.py" in capsys.readouterr().err

    monkeypatch.setattr("sys.argv", ["exporter_openapi.py"])
    assert exporteur.main() == 0
    monkeypatch.setattr("sys.argv", ["exporter_openapi.py", "--check"])
    assert exporteur.main() == 0
