"""Module 0 : image Docker, dépendances, compose et CI du service FastAPI.

Failles du rapport module 0 §1 couvertes : 24-25 (compose sans URL Django
ni DATABASE_URL, mot de passe superutilisateur transmis sans être lu), 27
(Dockerfile), 28 (dépendances de test dans l'image), 29 (CI).
Lecture des fichiers du dépôt (PyYAML est fourni par uvicorn[standard]).
"""
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
    """Faille 24-25 : URL Django, base en lecture seule et Redis base 2 ;
    plus aucun DB_* ni mot de passe superutilisateur."""
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
    """Faille 27."""
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
    """Faille 28."""
    requirements = _requirements("requirements.txt")
    names = {line.split("==")[0].split("[")[0].lower() for line in requirements}
    assert all("==" in line for line in requirements)
    assert {"asyncpg", "redis", "httpx", "fastapi"} <= names
    assert not {"pytest", "pytest-asyncio", "fakeredis", "python-multipart"} & names


def test_dev_requirements_extend_app_requirements():
    requirements = _requirements("requirements-dev.txt")
    assert requirements[0] == "-r requirements.txt"
    assert {line.split("==")[0] for line in requirements[1:]} == {"pytest", "fakeredis"}


def test_ci_installs_dev_requirements_once():
    """Faille 29 : plus de double installation."""
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "ci-fastapi.yml").read_text(encoding="utf-8"))
    commands = "\n".join(step.get("run", "") for step in workflow["jobs"]["test"]["steps"])
    assert "pip install -r requirements-dev.txt" in commands
    assert "pip install pytest" not in commands
    assert "create_app()" in commands
    assert workflow["env"]["ENVIRONMENT"] == "test"
