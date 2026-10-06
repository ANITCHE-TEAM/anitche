"""Image Docker, dépendances, compose et CI du service FastAPI.

Couvert : réglages transmis par le compose (URL Django, DATABASE_URL du
rôle en lecture seule, aucun mot de passe superutilisateur), Dockerfile,
dépendances de test hors de l'image, CI, options WebSocket d'uvicorn,
droits par colonne du rôle en lecture seule et droits sur les trois vues
publiques de la recherche, PUBLIC_BASE_URL et MEDIA_BASE_URL, job CI
« integration ».
Lecture des fichiers du dépôt (PyYAML est fourni par uvicorn[standard]).
"""
import re
from pathlib import Path

import pytest
import yaml

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent


def fastapi_service(compose_file: str) -> dict:
    compose = yaml.safe_load((REPO / "infra" / compose_file).read_text(encoding="utf-8"))
    return compose["services"]["backend-fastapi"]


@pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_compose_passes_the_right_settings(compose_file):
    """URL Django, base en lecture seule et Redis base 2 ; aucun DB_* ni
    mot de passe superutilisateur."""
    service = fastapi_service(compose_file)
    environment = service["environment"]

    assert environment["DJANGO_API_BASE_URL"].endswith(":8000/api")
    assert environment["DATABASE_URL"].startswith("postgresql://anitche_fastapi_ro:${FASTAPI_DB_PASSWORD")
    assert environment["REDIS_URL"] == "redis://redis:6379/2"
    assert not {"DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"} & set(environment)

    command = service["command"]
    assert "--factory app.main:create_app" in command
    assert "--no-proxy-headers" in command
    assert "python" in service["healthcheck"]["test"] and any("/health" in part for part in service["healthcheck"]["test"])
    assert service["depends_on"]["db"]["condition"] == "service_healthy"
    assert service["depends_on"]["redis"]["condition"] == "service_healthy"


def test_dev_compose_uses_the_host_accepted_by_django_in_dev():
    service = fastapi_service("docker-compose.yml")
    assert service["environment"]["DJANGO_API_BASE_URL"] == "http://anitche-backend:8000/api"
    assert service["environment"]["ENVIRONMENT"] == "dev"


def test_prod_compose():
    service = fastapi_service("docker-compose.prod.yml")
    environment = service["environment"]
    assert environment["ENVIRONMENT"] == "prod"
    assert environment["DJANGO_API_BASE_URL"] == "http://backend-django:8000/api"
    assert environment["TRUSTED_PROXY_COUNT"] == 1
    assert "--workers 2" in service["command"]
    assert "--reload" not in service["command"]
    assert "ports" not in service


def test_dockerfile_without_build_tools_root_or_reload():
    """Ni outils de compilation, ni root, ni --reload dans l'image."""
    dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    assert "apt-get" not in dockerfile
    assert "\nUSER app\n" in dockerfile
    cmd = next(line for line in dockerfile.splitlines() if line.startswith("CMD"))
    assert "--reload" not in cmd
    assert '"--no-proxy-headers"' in cmd


def _requirements(name: str) -> list[str]:
    lines = (BACKEND / name).read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def test_app_requirements_have_no_test_tools_and_are_pinned():
    """Dépendances de l'image épinglées, sans outil de test."""
    requirements = _requirements("requirements.txt")
    names = {line.split("==")[0].split("[")[0].lower() for line in requirements}
    assert all("==" in line for line in requirements)
    assert {"asyncpg", "redis", "httpx", "fastapi"} <= names
    assert not {"pytest", "pytest-asyncio", "fakeredis", "python-multipart"} & names


def test_dev_requirements_extend_app_requirements():
    requirements = _requirements("requirements-dev.txt")
    assert requirements[0] == "-r requirements.txt"
    assert {line.split("==")[0] for line in requirements[1:]} == {"pytest", "fakeredis", "pytest-timeout"}


def test_ci_installs_dev_requirements_once():
    """Une seule installation : requirements-dev.txt, sans pip install pytest à part."""
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "ci-fastapi.yml").read_text(encoding="utf-8"))
    commands = "\n".join(step.get("run", "") for step in workflow["jobs"]["test"]["steps"])
    assert "pip install -r requirements-dev.txt" in commands
    assert "pip install pytest" not in commands
    assert "create_app()" in commands
    assert workflow["env"]["ENVIRONMENT"] == "test"


# ------------------------------------------------------------ suivi GPS et recherche

WS_FLAGS = "--ws-max-size 8192 --ws-ping-interval 20 --ws-ping-timeout 20"


@pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_compose_bounds_websocket_messages_and_pings(compose_file):
    """Barrière extérieure d'uvicorn (8 Kio par message WebSocket), ping de 20 s."""
    assert WS_FLAGS in fastapi_service(compose_file)["command"]


def test_dockerfile_bounds_websocket_messages_and_pings():
    dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    cmd = next(line for line in dockerfile.splitlines() if line.startswith("CMD"))
    assert '"--ws-max-size", "8192", "--ws-ping-interval", "20", "--ws-ping-timeout", "20"' in cmd


SEARCH_VIEWS = {"catalogue_produit_public", "catalogue_categorie_publique", "catalogue_boutique_publique"}


def _readonly_sql_code() -> str:
    sql = (REPO / "infra" / "postgres" / "fastapi_readonly.sql").read_text(encoding="utf-8")
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))


def test_readonly_role_gets_exactly_the_tracking_columns():
    """SELECT par colonne, rien d'autre ; REVOKE ALL sur chaque table
    accordée (script idempotent). En plus : les trois vues publiques de la
    recherche (test suivant), aucune autre table."""
    code = _readonly_sql_code()
    grants = {
        table: {column.strip() for column in columns.split(",")}
        for columns, table in re.findall(r"GRANT SELECT \(([^)]*)\) ON TABLE (\w+) TO anitche_fastapi_ro;", code)
    }
    assert grants == {
        "livraison_livraison": {"id", "commande_id", "livreur_id", "status"},
        "commandes_commande": {"id", "client_id", "groupe_id"},
        "commandes_groupecommande": {"id", "livraison_latitude", "livraison_longitude"},
        "utilisateurs_utilisateur": {"id", "role", "is_active"},
    }
    for table in grants:
        assert f"REVOKE ALL ON TABLE {table} FROM anitche_fastapi_ro;" in code
    # Aucun droit sur tout le schéma, ni d'écriture ; seuls droits de table :
    # les trois vues de la recherche.
    assert len(re.findall(r"\bGRANT\b", code)) == len(grants) + len(SEARCH_VIEWS) + 2  # + CONNECT et USAGE
    assert "ALL TABLES" not in code and "DEFAULT PRIVILEGES" not in code
    assert not re.search(r"GRANT (INSERT|UPDATE|DELETE|TRUNCATE|ALL)", code)


def test_readonly_role_reads_only_the_public_catalogue_views():
    """SELECT sur les trois vues créées par la migration Django
    catalogue 0004, REVOKE ALL avant (idempotent) ; aucune table du
    catalogue ni des boutiques."""
    code = _readonly_sql_code()
    table_grants = set(re.findall(r"GRANT SELECT ON TABLE (\w+) TO anitche_fastapi_ro;", code))
    assert table_grants == SEARCH_VIEWS
    for view in SEARCH_VIEWS:
        assert f"REVOKE ALL ON TABLE {view} FROM anitche_fastapi_ro;" in code
    granted = re.findall(r"GRANT SELECT[^;]* ON TABLE (\w+)", code)
    assert not [table for table in granted if table.startswith(("catalogue_", "vendeurs_")) and table not in SEARCH_VIEWS]


@pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_compose_passes_search_urls(compose_file):
    """Adresses publiques de FastAPI et des médias."""
    environment = fastapi_service(compose_file)["environment"]
    assert {"PUBLIC_BASE_URL", "MEDIA_BASE_URL"} <= set(environment)


def test_search_urls_have_no_default_in_prod_and_local_values_in_dev():
    """Prod : aucune valeur par défaut (obligatoires, refusées par les
    réglages si absentes ou pas en https://) ; dev : services locaux."""
    prod = fastapi_service("docker-compose.prod.yml")["environment"]
    assert prod["PUBLIC_BASE_URL"] == "${PUBLIC_BASE_URL}"
    assert prod["MEDIA_BASE_URL"] == "${MEDIA_BASE_URL}"
    dev = fastapi_service("docker-compose.yml")["environment"]
    assert dev["PUBLIC_BASE_URL"] == "http://localhost:8001"
    assert dev["MEDIA_BASE_URL"] == "http://localhost:8000/media/"


def test_env_example_documents_https_search_urls():
    lines = (REPO / "infra" / ".env.example").read_text(encoding="utf-8").splitlines()
    values = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))
    assert values["PUBLIC_BASE_URL"] == "https://anitche.com/fast"
    assert values["MEDIA_BASE_URL"].startswith("https://")


def _workflow() -> dict:
    return yaml.safe_load((REPO / ".github" / "workflows" / "ci-fastapi.yml").read_text(encoding="utf-8"))


def test_ci_runs_unit_tests_without_services():
    commands = "\n".join(step.get("run", "") for step in _workflow()["jobs"]["test"]["steps"])
    assert '-m "not integration"' in commands
    assert "services" not in _workflow()["jobs"]["test"]


def test_ci_integration_job_uses_real_services_and_cannot_skip():
    """Vrais PostgreSQL et Redis, schéma des migrations Django,
    vrai script du rôle, un test sauté fait échouer le job."""
    workflow = _workflow()
    job = workflow["jobs"]["integration"]
    assert job["services"]["postgres"]["image"] == "postgres:16"
    assert job["services"]["redis"]["image"] == "redis:7-alpine"
    runs = [step.get("run", "") for step in job["steps"]]
    migrate = next(i for i, run in enumerate(runs) if "manage.py migrate" in run)
    role = next(i for i, run in enumerate(runs) if "infra/postgres/fastapi_readonly.sql" in run)
    tests = next(i for i, run in enumerate(runs) if "-m integration" in run)
    assert migrate < role < tests  # les tables existent avant le script
    env = job["steps"][tests]["env"]
    assert env["REQUIRE_INTEGRATION"] == "1"
    assert env["FASTAPI_TEST_DATABASE_URL"].startswith("postgresql://anitche_fastapi_ro:")
    assert env["FASTAPI_TEST_REDIS_URL"].endswith("/15")
    paths = workflow[True]["push"]["paths"]  # PyYAML lit la clé « on » comme True
    assert "infra/postgres/**" in paths and "backend-django/apps/*/migrations/**" in paths
