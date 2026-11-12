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

    # Shared secret for POST /api/events/ingest (X-Ingest-Token header).
    # The gateway and sandbox-facing /internal/* endpoints use this to push
    # events into the store. Dev default — override in real deployments.
    INGEST_TOKEN: str = "dev-ingest-token"

    # Root of the local artifact store: ./data/artifacts/<run_id>/<sandbox_id>/
    ARTIFACT_ROOT: Path = Path("./data/artifacts")

    # --- Orchestrator (F2) ---
    # HMAC secret for minting run/sandbox gateway creds — MUST match the
    # gateway service's GATEWAY_CRED_SECRET or sandbox traffic 401s.
    GATEWAY_CRED_SECRET: str = "dev-secret-change-me"
    # Optional override for the rubric-drafting model; empty => use each run's
    # participant model_name (keeps MVP single-model, metered uniformly).
    RUBRIC_MODEL: str = ""
    # Sandbox image spawned per harness config.
    SANDBOX_IMAGE: str = "heg-sandbox:latest"
    # Monitor loop cadence + hard cap around the evaluator call.
    MONITOR_POLL_SECONDS: float = 5.0
    EVAL_TIMEOUT_SECONDS: float = 1800.0
    # Host path of ./data as seen by the docker daemon — set by compose
    # (HOST_DATA_ROOT=${PWD}/data) so sandbox -v sources resolve on the host.
    # Empty (host-run dev / tests) => paths pass through verbatim.
    HOST_DATA_ROOT: str = ""
    # Vendored skill-pack presets; validated at submit + staged per sandbox.
    # Anchored to the repo root so it resolves inside the api image (/app)
    # and in host-run dev alike.
    SKILLPACKS_DIR: Path = Path(__file__).resolve().parents[1] / "skillpacks"

    LOG_LEVEL: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
