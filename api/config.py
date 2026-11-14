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
    # Token allowance for orchestrator-minted creds (rubric draft + per-eval
    # judge). Carried as max_tokens = tokens already spent + this allowance,
    # so it is independent of participant spend against the run budget.
    EVAL_TOKEN_BUDGET: int = 200_000
    # Extra judge attempts per sandbox when the eval produced zero Score rows
    # (missing/invalid scores.json) — small judge models flake ~1 in 3 evals;
    # one retry is a cheap coin-flip recover. Infra exceptions are not retried.
    EVAL_SCORE_RETRIES: int = 1
    # Host path of ./data as seen by the docker daemon — set by compose
    # (HOST_DATA_ROOT=${PWD}/data) so sandbox -v sources resolve on the host.
    # Empty (host-run dev / tests) => paths pass through verbatim.
    HOST_DATA_ROOT: str = ""
    # Vendored skill-pack presets; validated at submit + staged per sandbox.
    # Anchored to the repo root so it resolves inside the api image (/app)
    # and in host-run dev alike.
    SKILLPACKS_DIR: Path = Path(__file__).resolve().parents[1] / "skillpacks"
    # Root for USER-IMPORTED skill packs (F11). None => derived from
    # ARTIFACT_ROOT as <data>/skillpacks (sibling of artifacts/ + sandboxes/,
    # so compose's ./data:/data mount + SandboxRuntime.to_host_path cover it).
    # Set explicitly to relocate the imported-packs root.
    IMPORTED_SKILLPACKS_DIR: Path | None = None
    # Optional GitHub token for skillpack discovery/import — raises the
    # unauthenticated 60/hr api limit and unlocks private repos (best-effort;
    # codeload accepts a Bearer token). Public repos work without it.
    GITHUB_TOKEN: str = ""

    # --- Artifact preview (F12) ---
    # Static-preview container image (`python -m http.server`); shares the
    # python:3.12-slim base with the api/sandbox images — no extra pull.
    PREVIEW_IMAGE: str = "python:3.12-slim"
    # Dedicated bridge for preview containers (only peers = other previews).
    # Can't be --internal: Docker drops published ports on internal networks.
    PREVIEW_NETWORK: str = "heg-preview"
    # Auto-stop TTL for preview containers + the reaper's sweep cadence.
    PREVIEW_TTL_SECONDS: int = 900
    PREVIEW_REAP_SECONDS: float = 30.0
    # Published-port bind address — localhost only; previews are not LAN-visible.
    PREVIEW_BIND_HOST: str = "127.0.0.1"
    # --- Desktop previews (executes candidate code — pygame via noVNC) ---
    # Runner image: repo-built, TigerVNC + noVNC/websockify + pygame-ce.
    PREVIEW_DESKTOP_IMAGE: str = "heg-preview-desktop:latest"
    # Sidecar that publishes the runner's noVNC port (dumb TCP pipe).
    PREVIEW_PROXY_IMAGE: str = "alpine/socat:1.8.1.3"
    # Internal net for desktop runners — zero egress; only peer = own proxy.
    PREVIEW_DESKTOP_NETWORK: str = "heg-desktop"
    # Resource bounds on executed candidate code.
    PREVIEW_DESKTOP_CPUS: str = "1"
    PREVIEW_DESKTOP_MEMORY: str = "512m"
    PREVIEW_DESKTOP_PIDS: int = 256

    LOG_LEVEL: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
