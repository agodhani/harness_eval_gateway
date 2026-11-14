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
os.environ.setdefault("UPSTREAM_PROVIDER", "ollama")
os.environ.setdefault("ARTIFACT_ROOT", str(ROOT / "data" / "artifacts"))
os.environ.setdefault("LOG_LEVEL", "WARNING")
# PR7: the exec signal collector lives in the default COLLECTORS registry —
# pin it off for the suite so a stray collect_signals() can't spawn docker
# containers on a dev machine. Docker-marked tests enable it explicitly.
os.environ.setdefault("SCORING_EXEC", "0")

from fastapi.testclient import TestClient

from api.main import app


@pytest.fixture()
def client() -> TestClient:
    """FastAPI TestClient bound to the app (sqlite :memory: DB)."""
    return TestClient(app)
