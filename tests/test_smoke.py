"""F0 smoke tests — the app boots and the health surface responds."""

import pytest
from fastapi.testclient import TestClient


def test_app_imports() -> None:
    from api.main import app  # noqa: F401


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_db(client: TestClient) -> None:
    resp = client.get("/health/db")
    if resp.status_code == 503:
        pytest.skip("database unavailable")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
