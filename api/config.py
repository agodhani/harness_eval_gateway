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

    # --- Upstream providers (consumed by the gateway service) ---
    # Declared here so the api/orchestrator process loads the same shared
    # .env cleanly and can introspect which provider a model routes to
    # (e.g. a future submit-form hint). The provider API KEYS are
    # deliberately NOT declared — only the gateway service loads them; they
    # must never reach sandboxes, clients, or this process.
    UPSTREAM_PROVIDER: str = "ollama"
    PROVIDER_ROUTES: str = ""

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
    # Per-request output cap (chat-completions max_tokens) for the rubric
    # draft call. Thinking models burn output tokens on <think> blocks — the
    # old 2048 cap truncated rubric JSON mid-document on qwen3-class models.
    RUBRIC_MAX_TOKENS: int = 8192
    # Per-request output cap for the eval judge's chat calls (miniloop
    # MAX_TOKENS in the eval sandbox). Rationale-heavy scores.json plus
    # thinking-model overhead need headroom over the 4096 participant
    # default — truncation yields missing/invalid scores.json.
    EVAL_JUDGE_MAX_TOKENS: int = 8192
    # Host path of ./data as seen by the docker daemon — set by compose
    # (HOST_DATA_ROOT=${PWD}/data) so sandbox -v sources resolve on the host.
    # Empty (host-run dev / tests) => paths pass through verbatim.
    HOST_DATA_ROOT: str = ""
    # Vendored skill-pack presets; validated at submit + staged per sandbox.
    # Anchored to the repo root so it resolves inside the api image (/app)
    # and in host-run dev alike.
    SKILLPACKS_DIR: Path = Path(__file__).resolve().parents[1] / "skillpacks"
    # PR22 vendored task presets (<root>/<slug>.md — frontmatter + prompt
    # body). Same repo-root anchoring: the api image bakes the repo, so
    # adding a committed preset needs no extra config.
    TASK_PRESETS_DIR: Path = Path(__file__).resolve().parents[1] / "task_presets"
    # PR26 vendored benchmark suites (<root>/<slug>/suite.yaml + optional
    # py_test dirs). Same repo-root anchoring as task_presets.
    BENCHMARK_SUITES_DIR: Path = (
        Path(__file__).resolve().parents[1] / "benchmark_suites"
    )
    # PR23 vendored rubric templates (<root>/<name>.json). Same repo-root
    # anchoring; the DB rubric_templates table holds the user side.
    RUBRIC_TEMPLATES_DIR: Path = (
        Path(__file__).resolve().parents[1] / "rubric_templates"
    )
    # Root for USER-IMPORTED skill packs (F11). None => derived from
    # ARTIFACT_ROOT as <data>/skillpacks (sibling of artifacts/ + sandboxes/,
    # so compose's ./data:/data mount + SandboxRuntime.to_host_path cover it).
    # Set explicitly to relocate the imported-packs root.
    IMPORTED_SKILLPACKS_DIR: Path | None = None
    # Optional GitHub token for skillpack discovery/import — raises the
    # unauthenticated 60/hr api limit and unlocks private repos (best-effort;
    # codeload accepts a Bearer token). Public repos work without it.
    GITHUB_TOKEN: str = ""

    # --- Task-input seeds (PR24) ---
    # Root of staged task-input archives: None => <data>/uploads (sibling of
    # artifacts/ + sandboxes/ + skillpacks/, so the compose ./data mount
    # covers it). Set explicitly to relocate.
    UPLOADS_DIR: Path | None = None
    # Upload POST byte cap — applies to the raw archive file.
    UPLOAD_MAX_BYTES: int = 256 * 1024 * 1024
    # Extraction caps — an archive that passed the upload cap can still be a
    # decompression bomb; these bound the materialized seed tree.
    SEED_MAX_BYTES: int = 256 * 1024 * 1024
    SEED_MAX_FILES: int = 20_000
    # Wall-clock cap on the whole git fetch+checkout for a repo seed.
    SEED_GIT_TIMEOUT_SECONDS: float = 300.0

    # --- Objective scoring (PR7) ---
    # Exec-signal kill switch — when false the collector reports "skipped"
    # instead of running candidate build/tests/coverage inside the locked
    # heg-exec container. Read via os.environ first (compose passes it
    # through) so an explicit env var beats .env, then this setting.
    SCORING_EXEC: bool = True
    # Repo-built image ExecRunner spawns for exec scoring (never pulled):
    #   docker build -f sandbox/images/exec.Dockerfile -t heg-exec:latest .
    EXEC_IMAGE: str = "heg-exec:latest"
    # PR8 generated-test signal kill switch — when false the evaluator skips
    # LLM test-suite generation entirely (no gateway call, no exec). Same
    # env-first resolution as SCORING_EXEC.
    SCORING_TESTGEN: bool = True

    # --- Run pacing (PR15) ---
    # One active run at a time: approvals landing while a run executes (or
    # is already queued) park in status="queued" and promote FIFO when the
    # slot frees. No knob — it's the run state machine.
    # Spawn memory gate: spawn_one waits for >= this much free host/VM
    # memory before `docker run` (MiB). Probed via /proc/meminfo, then
    # sysconf, then docker info+stats (unreadable probe => gate off).
    # 0 disables. Default 2 GiB — headroom for the model + siblings.
    SPAWN_MIN_FREE_MEM_MB: int = 2048
    # Poll cadence + cap for the gate wait. An expired wait fails THAT
    # sandbox like any spawn error — and collapses remaining configs' waits
    # to a single probe, so sustained pressure can't hang a run for N×cap.
    SPAWN_MEM_POLL_SECONDS: float = 5.0
    SPAWN_MEM_WAIT_SECONDS: float = 3600.0
    # Local-run sandbox cap: when EVERY participant model on a run resolves
    # to a local provider (ollama), at most this many cells' containers are
    # active at once — extra cells park as Sandbox.status="queued" before
    # `docker run` and promote FIFO when siblings go terminal. 2 default:
    # a small local model can't honestly serve N parallel agents anyway,
    # and 2 keeps host RAM+contention sane. 0 disables.
    SANDBOX_MAX_CONCURRENT_LOCAL: int = 2

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
    # Results-GET preview probe cache TTL — the per-sandbox workspace scan is
    # reused this long (the workspace only changes on re-snapshot; 0 disables).
    PREVIEW_SCAN_TTL_SECONDS: float = 30.0
    # --- Desktop previews (executes candidate code — GUI via noVNC) ---
    # Runner image: repo-built, TigerVNC + noVNC/websockify + baked GUI
    # stacks (pygame-ce, pyglet, arcade, tkinter).
    PREVIEW_DESKTOP_IMAGE: str = "heg-preview-desktop:latest"
    # --- python-server previews (executes candidate code — web app) ---
    # Runner image: repo-built, common web frameworks baked in (flask,
    # fastapi/uvicorn, django, aiohttp, bottle, tornado, gunicorn).
    PREVIEW_SERVER_IMAGE: str = "heg-preview-server:latest"
    # Sidecar that publishes the runner's port (dumb TCP pipe).
    PREVIEW_PROXY_IMAGE: str = "alpine/socat:1.8.1.3"
    # Internal net for executed-code runners — zero egress; peers = own proxy
    # + other ephemeral preview runners, none of which can egress.
    PREVIEW_DESKTOP_NETWORK: str = "heg-desktop"
    # Resource bounds on executed candidate code (desktop + python-server).
    PREVIEW_DESKTOP_CPUS: str = "1"
    PREVIEW_DESKTOP_MEMORY: str = "512m"
    PREVIEW_DESKTOP_PIDS: int = 256

    # --- Artifact thumbnails (PR4) ---
    # Headless-chromium capture image — repo-built from
    # sandbox/images/thumbnail-capture.Dockerfile. Joins heg-preview and
    # screenshots the preview stack's in-network URL.
    PREVIEW_THUMB_IMAGE: str = "heg-preview-thumb:latest"
    # Bounds on one capture: wait-for-serve, the chromium run itself, and
    # the TTL on the ephemeral thumb-purpose preview stack (safety net if
    # the api dies mid-capture).
    THUMB_READY_SECONDS: float = 30.0
    THUMB_CAPTURE_SECONDS: float = 45.0
    THUMB_PREVIEW_TTL_SECONDS: float = 300.0
    # Pre-capture delay so a desktop app's VNC framebuffer has content —
    # Xvnc is loopback-only inside the runner, so noVNC is the only window.
    THUMB_DESKTOP_SETTLE_SECONDS: float = 8.0
    THUMB_SETTLE_SECONDS: float = 0.5
    # Viewport for the screenshot.
    THUMB_WIDTH: int = 1280
    THUMB_HEIGHT: int = 800
    # chromium --virtual-time-budget per type — desktop (noVNC attach +
    # framebuffer render) gets the longer budget.
    THUMB_BUDGET_MS: int = 10_000
    THUMB_DESKTOP_BUDGET_MS: int = 20_000

    LOG_LEVEL: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
