"""Gateway test fixtures.

- Creates the db schema on the gateway's own sqlite :memory: engine
  (``gateway.deps`` builds a separate engine from the api's — each
  ``sqlite:///:memory:`` engine is its own database).
- ``gw_client`` — TestClient bound to the gateway FastAPI app.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from db.models import Base
from gateway.deps import get_session_factory
from gateway.main import app


@pytest.fixture(autouse=True)
def _schema() -> None:
    sf = get_session_factory()
    Base.metadata.create_all(sf.kw["bind"])


@pytest.fixture()
def gw_client() -> TestClient:
    return TestClient(app)
