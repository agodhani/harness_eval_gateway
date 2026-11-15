"""FastAPI control-plane app factory.

Feature routers (runs, events, results, ...) are added per FEATURE_MAP.md;
the web UI calls them under `/api/*` (Next.js rewrites or a router prefix —
decided in F1).

Lifespan runs ``orchestrator.runner.startup_sweep`` on boot (reconcile DB
state after an api restart — in-process tasks die with the process) and
``runner.shutdown`` on exit (cancel in-flight orchestration tasks). It also
starts/stops the artifact-preview TTL reaper (F12).
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.config import get_settings
from api.routers import (
    analytics,
    events,
    harnesses,
    health,
    models,
    preview,
    results,
    runs,
    skillpacks,
)
from orchestrator import runner


@asynccontextmanager
async def lifespan(app: FastAPI):
    await runner.startup_sweep()
    preview.start_reaper()
    yield
    await runner.shutdown()
    await preview.stop_reaper()


def create_app() -> FastAPI:
    get_settings()  # fail fast on bad env config
    app = FastAPI(title="harness_eval_gateway", version="0.1.0", lifespan=lifespan)

    # Local dev: allow the Next.js dev server on any localhost port.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(harnesses.router)
    app.include_router(models.router)
    app.include_router(events.router)
    app.include_router(runs.router)
    app.include_router(results.router)
    app.include_router(analytics.router)
    app.include_router(preview.router)
    app.include_router(skillpacks.router)
    return app


app = create_app()
