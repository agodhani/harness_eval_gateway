"""F0 smoke tests — the app boots and the health surface responds."""

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient


def test_app_imports() -> None:
    from api.main import app  # noqa: F401


def test_health(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_compose_api_waits_for_gateway_to_listen() -> None:
    compose = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    services = yaml.safe_load(compose.read_text())["services"]
    gateway = services["gateway"]

    assert services["api"]["depends_on"]["gateway"]["condition"] == "service_healthy"
    assert gateway["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert "api" not in gateway["depends_on"]
    assert "http://localhost:8080/health" in gateway["healthcheck"]["test"][1]


def test_health_db(client: TestClient) -> None:
    resp = client.get("/health/db")
    if resp.status_code == 503:
        pytest.skip("database unavailable")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
