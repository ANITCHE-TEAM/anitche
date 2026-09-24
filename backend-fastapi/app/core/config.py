from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "ANITCHE Fast Engine"
    app_version: str = "1.0.0"
    api_prefix: str = ""
    debug: bool = True

    # CORS
    allowed_origins: List[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://anitche.ci",
    ]

    # Connexions
    database_url: str = "postgresql+asyncpg://postgres:password@localhost:5432/anitche"
    redis_url: str = "redis://localhost:6379/1"
    django_api_base_url: str = "http://localhost:8000/api"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()