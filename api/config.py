"""Application settings, loaded from environment / .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Postgres DSN used by SQLAlchemy (psycopg v3 driver).
    # Host port 5433 — compose publishes postgres there to avoid local conflicts.
    DATABASE_URL: str = "postgresql+psycopg://postgres:postgres@localhost:5433/harness_eval"

    # Ollama upstream for the AI gateway (host install for MVP).
    OLLAMA_BASE_URL: str = "http://localhost:11434"

    # Where the custom AI gateway will listen (F3). Host port 8081 — 8080 is
    # taken by another local project.
    GATEWAY_URL: str = "http://localhost:8081"

    # Root of the local artifact store: ./data/artifacts/<run_id>/<sandbox_id>/
    ARTIFACT_ROOT: Path = Path("./data/artifacts")

    LOG_LEVEL: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
