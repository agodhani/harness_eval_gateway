"""Shared pytest fixtures.

- Puts the repo root on sys.path so `api`, `db`, ... import as packages.
- Points DATABASE_URL at in-memory sqlite BEFORE `api.main` is imported, so
  `api.config.Settings` picks it up (settings are read lazily per process).
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("OLLAMA_BASE_URL", "http://localhost:11434")
os.environ.setdefault("GATEWAY_URL", "http://localhost:8081")
# Pin the gateway upstream to ollama so a developer's .env provider switch
# (e.g. UPSTREAM_PROVIDER=openrouter for live testing) can't leak into tests.
# Same for PROVIDER_ROUTES — model-glob overrides must not reroute tests.
os.environ.setdefault("UPSTREAM_PROVIDER", "ollama")
os.environ.setdefault("PROVIDER_ROUTES", "")
# Same for the PR18 replay cache — a dev's GATEWAY_CACHE_ENABLED=true must
# not make tests silently replay stored responses.
os.environ.setdefault("GATEWAY_CACHE_ENABLED", "false")
os.environ.setdefault("ARTIFACT_ROOT", str(ROOT / "data" / "artifacts"))
os.environ.setdefault("LOG_LEVEL", "WARNING")

from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture()
def client() -> TestClient:
    """FastAPI TestClient bound to the app (sqlite :memory: DB)."""
    return TestClient(app)
