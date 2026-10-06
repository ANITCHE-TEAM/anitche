"""Exporte le schéma OpenAPI du service FastAPI dans backend-fastapi/openapi.json.

    python scripts/exporter_openapi.py           # écrit le fichier
    python scripts/exporter_openapi.py --check   # échoue s'il n'est pas à jour

Le fichier est la référence du frontend pour les routes `/fast` (comme
`backend-django/schema.yaml` pour `/api`) : `/openapi.json` n'existe pas en
production. Le schéma est identique d'un poste à l'autre : les réglages sont
figés ici et aucune variable d'environnement ni `.env` n'est lu.
"""
import argparse
import json
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
FICHIER = RACINE / "openapi.json"
COMMANDE = "python scripts/exporter_openapi.py"

sys.path.insert(0, str(RACINE))

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource  # noqa: E402

from app.core.settings import Settings  # noqa: E402
from app.main import create_app  # noqa: E402


class ReglagesFiges(Settings):
    """Seules les valeurs passées au constructeur comptent."""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        **_autres_sources: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (init_settings,)


def generer_texte() -> str:
    """Schéma de l'application construite avec les réglages par défaut du
    code, en environnement `test` (documentation active, aucune exigence de
    prod). `app.openapi()` n'exécute pas le lifespan : aucune ressource."""
    settings = ReglagesFiges(environment="test")
    schema = create_app(settings).openapi()
    # Ordre des clés conservé (pas de sort_keys) : celui des routes.
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def lire_fichier() -> str | None:
    if not FICHIER.exists():
        return None
    return FICHIER.read_bytes().decode("utf-8").replace("\r\n", "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="échoue si le fichier versionné n'est pas à jour")
    args = parser.parse_args()

    texte = generer_texte()
    if args.check:
        if lire_fichier() == texte:
            print(f"{FICHIER.name} est à jour.")
            return 0
        print(
            f"{FICHIER.name} n'est pas à jour. Depuis backend-fastapi, lancer :\n"
            f"    {COMMANDE}\n"
            "puis versionner le fichier modifié.",
            file=sys.stderr,
        )
        return 1

    # Écriture en octets : toujours LF, même sous Windows.
    FICHIER.write_bytes(texte.encode("utf-8"))
    print(f"{FICHIER.name} écrit ({len(texte.splitlines())} lignes).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
