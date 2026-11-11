"""Gateway settings, loaded from environment / .env file.

The gateway is a standalone FastAPI app (own process, port 8080 in compose /
8081 on the host) but shares the repo's ``db/`` models for budget accounting.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Postgres DSN for budget lookups against the events table. Inside compose
    # this points at `postgres:5432`; host-run dev uses localhost:5433.
    DATABASE_URL: str = "postgresql+psycopg://postgres:postgres@localhost:5433/harness_eval"

    # OpenAI-compatible upstream. Default assumes running inside compose where
    # host.docker.internal reaches the host's Ollama; override for host-run dev
    # (e.g. OLLAMA_UPSTREAM_URL=http://localhost:11434).
    OLLAMA_UPSTREAM_URL: str = "http://host.docker.internal:11434"

    # Control-plane ingest endpoint + shared secret for metering events.
    # Inside compose: http://api:8000 ; host-run dev: http://localhost:8001.
    API_INGEST_URL: str = "http://api:8000"
    INGEST_TOKEN: str = "dev-ingest-token"

    # HMAC secret for `heg.*` bearer credentials (see gateway/creds.py).
    GATEWAY_CRED_SECRET: str = "dev-secret-change-me"

    # Fallback per-run token budget when the credential carries no max_tokens
    # claim. A cred's own max_tokens overrides this.
    DEFAULT_RUN_TOKEN_BUDGET: int = 200_000

    UPSTREAM_TIMEOUT: float = 300.0
    INGEST_TIMEOUT: float = 10.0


@lru_cache
def get_settings() -> GatewaySettings:
    return GatewaySettings()
