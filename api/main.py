"""FastAPI control-plane app factory.

F0 exposes only health endpoints. Feature routers (runs, events, results, ...)
are added per FEATURE_MAP.md; the web UI calls them under `/api/*` (Next.js
rewrites or a router prefix — decided in F1).
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.config import get_settings
from api.routers import events, health


def create_app() -> FastAPI:
    get_settings()  # fail fast on bad env config
    app = FastAPI(title="harness_eval_gateway", version="0.1.0")

    # Local dev: allow the Next.js dev server on any localhost port.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(events.router)
    return app


app = create_app()
