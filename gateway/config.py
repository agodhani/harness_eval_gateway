"""Gateway settings, loaded from environment / .env file.

The gateway is a standalone FastAPI app (own process, port 8080 in compose /
8081 on the host) but shares the repo's ``db/`` models for budget accounting.
"""

import fnmatch
import json
from functools import lru_cache
from typing import Any, Literal, cast, get_args

from pydantic_settings import BaseSettings, SettingsConfigDict

# Upstream providers the gateway can forward to.
ProviderName = Literal["ollama", "openrouter", "openai", "anthropic"]
_PROVIDER_NAMES: frozenset[str] = frozenset(get_args(ProviderName))

# Hosted providers → the settings field carrying their API key (`ollama`
# needs no key and is omitted).
_PROVIDER_KEY_FIELDS: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


@lru_cache(maxsize=8)
def _parse_provider_routes(raw: str) -> dict[str, ProviderName]:
    """Parse + validate a ``PROVIDER_ROUTES`` JSON string.

    ``provider_for_model`` resolves per request, so the parse is cached on
    the raw string (key validation still fails fast at boot via
    ``model_post_init``). Small cache — the value is process config, but
    bound it anyway; callers must not mutate the returned dict.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"PROVIDER_ROUTES is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) for k in parsed
    ):
        raise ValueError(
            "PROVIDER_ROUTES must be a JSON object of model-glob → provider"
        )
    for glob, provider in parsed.items():
        if provider not in _PROVIDER_NAMES:
            raise ValueError(
                f"PROVIDER_ROUTES[{glob!r}] routes to unknown provider "
                f"{provider!r} (one of {sorted(_PROVIDER_NAMES)})"
            )
    return cast(dict[str, ProviderName], parsed)


def require_provider(value: str) -> ProviderName:
    """``value`` as a ``ProviderName`` (case/space-tolerant).

    ``x-heg-provider`` override values get the same validation as
    ``PROVIDER_ROUTES`` targets; unknown names raise ``ValueError``.
    """
    name = value.strip().lower()
    if name in _PROVIDER_NAMES:
        return cast(ProviderName, name)
    raise ValueError(
        f"unknown provider {value!r} (one of {sorted(_PROVIDER_NAMES)})"
    )


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Postgres DSN for budget lookups against the events table. Inside compose
    # this points at `postgres:5432`; host-run dev uses localhost:5433.
    DATABASE_URL: str = "postgresql+psycopg://postgres:postgres@localhost:5433/harness_eval"

    # Default upstream provider for every request (PROVIDER_ROUTES overrides
    # per model).
    # `ollama`: local Ollama at OLLAMA_UPSTREAM_URL (default).
    # `openrouter`: OpenRouter's OpenAI-compatible API (OPENROUTER_API_KEY;
    #   free `:free` ids work for testing without a local Ollama).
    # `openai`: api.openai.com (OPENAI_API_KEY).
    # `anthropic`: api.anthropic.com (ANTHROPIC_API_KEY).
    # Endpoint coverage differs — endpoints a provider doesn't implement
    # return its native 404 verbatim:
    #   ollama:     chat/completions + messages(+count_tokens) + responses + models
    #   openrouter: chat/completions + models
    #   openai:     chat/completions + responses + models (no /v1/messages)
    #   anthropic:  messages(+count_tokens) + models (no chat-completions/responses)
    UPSTREAM_PROVIDER: ProviderName = "ollama"
    # Optional per-model routing — a JSON object of model glob → provider:
    # '{"claude-*": "anthropic", "gpt-*": "openai"}'. Globs match
    # case-insensitively in declaration order; first hit wins, unmatched
    # models resolve UPSTREAM_PROVIDER. Lets one gateway e.g. serve
    # claude_code sandboxes from Anthropic while the rest stays on Ollama.
    PROVIDER_ROUTES: str = ""
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_BASE_URL: str = "https://api.anthropic.com/v1"
    # Anthropic requires an explicit protocol-version header on every request.
    ANTHROPIC_VERSION: str = "2023-06-01"

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
        # provider_routes() parses + validates the JSON so malformed config
        # fails at boot, not mid-run. Every provider a request may resolve to
        # (default or routed) must have its API key.
        for provider in {self.UPSTREAM_PROVIDER, *self.provider_routes().values()}:
            field = _PROVIDER_KEY_FIELDS.get(provider)
            if field is not None and not getattr(self, field):
                raise ValueError(f"provider {provider!r} requires {field}")

    def provider_routes(self) -> dict[str, ProviderName]:
        """Parsed ``PROVIDER_ROUTES``: ``{model_glob: provider}``.

        Declaration order is preserved — the first matching glob wins.
        ``{}`` when unset/blank. Cached (see ``_parse_provider_routes``) —
        do not mutate the returned dict.
        """
        return _parse_provider_routes(self.PROVIDER_ROUTES.strip())

    def provider_for_model(self, model: str | None) -> ProviderName:
        """Resolve the upstream provider serving ``model``.

        ``PROVIDER_ROUTES`` globs are matched case-insensitively in
        declaration order; the first hit wins. No model / no hit falls back
        to the process-wide ``UPSTREAM_PROVIDER``.
        """
        if isinstance(model, str) and model:
            lowered = model.lower()
            for glob, provider in self.provider_routes().items():
                if fnmatch.fnmatchcase(lowered, glob.lower()):
                    return provider
        return self.UPSTREAM_PROVIDER

    def upstream_base_url_for(self, provider: ProviderName) -> str:
        """httpx client base for ``provider``.

        httpx merges base path + request path verbatim — the proxy's request
        paths already carry the "/v1/..." prefix while hosted providers serve
        under "<root>/v1/..." ("/api/v1/..." for OpenRouter), so the client
        base is the API root, not ".../v1".
        """
        if provider == "ollama":
            return self.OLLAMA_UPSTREAM_URL.rstrip("/")
        base = {
            "openrouter": self.OPENROUTER_BASE_URL,
            "openai": self.OPENAI_BASE_URL,
            "anthropic": self.ANTHROPIC_BASE_URL,
        }[provider].rstrip("/")
        return base.removesuffix("/v1") + "/"

    def upstream_headers_for(self, provider: ProviderName) -> dict[str, str]:
        """Auth/version headers for the upstream client; Ollama needs none."""
        if provider == "openrouter":
            return {"Authorization": f"Bearer {self.OPENROUTER_API_KEY}"}
        if provider == "openai":
            return {"Authorization": f"Bearer {self.OPENAI_API_KEY}"}
        if provider == "anthropic":
            return {
                "x-api-key": self.ANTHROPIC_API_KEY,
                "anthropic-version": self.ANTHROPIC_VERSION,
            }
        return {}

    # --- default-provider accessors (pre-routing callers) ---

    @property
    def upstream_base_url(self) -> str:
        return self.upstream_base_url_for(self.UPSTREAM_PROVIDER)

    @property
    def upstream_headers(self) -> dict[str, str]:
        return self.upstream_headers_for(self.UPSTREAM_PROVIDER)

    @property
    def inject_ollama_defaults(self) -> bool:
        """Whether the DEFAULT provider takes Ollama-only body params
        (num_ctx, keep_alive). Hosted providers may reject unknown fields, so
        the gateway only adds these when the upstream is Ollama. Per-request
        code resolves the routed provider via ``provider_for_model`` instead.
        """
        return self.UPSTREAM_PROVIDER == "ollama"


@lru_cache
def get_settings() -> GatewaySettings:
    return GatewaySettings()
