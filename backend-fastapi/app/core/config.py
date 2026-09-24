from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://postgres:password@localhost:5432/anitche"

    class Config:
        env_file = ".env"

settings = Settings()