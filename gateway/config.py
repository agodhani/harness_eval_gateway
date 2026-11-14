"""Gateway settings, loaded from environment / .env file.

The gateway is a standalone FastAPI app (own process, port 8080 in compose /
8081 on the host) but shares the repo's ``db/`` models for budget accounting.
"""

from functools import lru_cache
from typing import Any, Literal

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

    # Upstream provider — process-wide switch (per-model routing is post-MVP).
    # `ollama`: local Ollama at OLLAMA_UPSTREAM_URL (default).
    # `openrouter`: OpenRouter's OpenAI-compatible API; requires
    # OPENROUTER_API_KEY. Useful for testing with hosted free models
    # (`:free`-suffixed ids) when no local Ollama is running.
    UPSTREAM_PROVIDER: Literal["ollama", "openrouter"] = "ollama"
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

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

    def model_post_init(self, __context: Any, /) -> None:
        if self.UPSTREAM_PROVIDER == "openrouter" and not self.OPENROUTER_API_KEY:
            raise ValueError(
                "UPSTREAM_PROVIDER=openrouter requires OPENROUTER_API_KEY"
            )

    @property
    def upstream_base_url(self) -> str:
        if self.UPSTREAM_PROVIDER == "openrouter":
            base = self.OPENROUTER_BASE_URL.rstrip("/")
            # httpx merges base path + request path verbatim — the proxy's
            # request paths already carry the "/v1/..." prefix while
            # OpenRouter serves under "/api/v1/...", so the client base is
            # the API root ("/api/"), not the documented "/api/v1".
            base = base.removesuffix("/v1")
            return base + "/"
        return self.OLLAMA_UPSTREAM_URL.rstrip("/")

    @property
    def upstream_headers(self) -> dict[str, str]:
        """Auth headers for the upstream client; Ollama needs none."""
        if self.UPSTREAM_PROVIDER == "openrouter":
            return {"Authorization": f"Bearer {self.OPENROUTER_API_KEY}"}
        return {}

    @property
    def inject_ollama_defaults(self) -> bool:
        """Whether to inject Ollama-only body params (num_ctx, keep_alive).

        OpenAI-compatible hosted providers may reject unknown fields, so the
        gateway only adds these when the upstream is Ollama.
        """
        return self.UPSTREAM_PROVIDER == "ollama"


@lru_cache
def get_settings() -> GatewaySettings:
    return GatewaySettings()
