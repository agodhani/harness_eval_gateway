"""PR28 — /api/prices: user rate CRUD + OpenRouter catalog sync.

Same sqlite + session-seeding pattern as test_analytics.py. The sync test
injects an httpx.MockTransport as the api→gateway client (dependency
override) — no live gateway/provider. Globs are namespaced ``pr28-*`` so a
row can't accidentally reprice models other tests use, and each test
deletes what it created (the api's :memory: sqlite is shared per-process).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from api.config import get_settings
from api.deps import get_session_factory
from api.main import app
from api.routers.models import get_gateway_client
from db.models import Base


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    sf = get_session_factory()
    Base.metadata.create_all(sf.kw["bind"])
    return sf


@pytest.fixture()
def cleanup_globs(session_factory: sessionmaker[Session]):
    """Track created globs and purge the file's ``pr28-*`` namespace
    afterwards — the shared :memory: DB outlives one test and a stray row
    (sync creates catalog ids a test may not have tracked) would reprice
    or pollute assertions downstream."""
    yield []
    with session_factory() as s:
        from db.models import ModelPriceRow

        s.execute(
            ModelPriceRow.__table__.delete().where(
                ModelPriceRow.model_glob.like("pr28-%")
            )
        )
        s.commit()


def _put(client: TestClient, **body: Any) -> httpx.Response:
    return client.put("/api/prices", json=body)


# ---------------------------------------------------------------------------
# GET / PUT / DELETE round-trip
# ---------------------------------------------------------------------------


def test_get_prices_lists_static_layer(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    resp = client.get("/api/prices")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["prices"], list)
    assert body["static"]["qwen3:8b"]["local"] is True
    # tests pin UPSTREAM_PROVIDER=ollama → no catalog sync available
    assert body["sync_available"] is False


def test_put_then_get_round_trip(
    client: TestClient, session_factory: sessionmaker[Session], cleanup_globs
) -> None:
    glob = "pr28-mymodel-*"
    cleanup_globs.append(glob)
    resp = _put(
        client,
        model_glob=glob,
        input_per_mtok_usd=1.5,
        output_per_mtok_usd=3.0,
        cache_read_per_mtok_usd=0.15,
        local=False,
    )
    assert resp.status_code == 200
    created = resp.json()
    assert created["model_glob"] == glob
    assert created["source"] == "user"
    assert created["cache_read_per_mtok_usd"] == 0.15

    body = client.get("/api/prices").json()
    row = next(r for r in body["prices"] if r["model_glob"] == glob)
    assert row["id"] == created["id"]
    assert row["input_per_mtok_usd"] == 1.5


def test_put_upserts_same_glob_keeps_row(
    client: TestClient, session_factory: sessionmaker[Session], cleanup_globs
) -> None:
    glob = "pr28-upsert-*"
    cleanup_globs.append(glob)
    first = _put(client, model_glob=glob, input_per_mtok_usd=1.0).json()
    second = _put(client, model_glob=glob, input_per_mtok_usd=2.5).json()
    assert second["id"] == first["id"]  # same row updated, not duplicated
    assert second["input_per_mtok_usd"] == 2.5
    globs = [r["model_glob"] for r in client.get("/api/prices").json()["prices"]]
    assert globs.count(glob) == 1


def test_put_rejects_negative_rate(client: TestClient) -> None:
    resp = _put(client, model_glob="pr28-neg-*", input_per_mtok_usd=-1.0)
    assert resp.status_code == 422


def test_put_rejects_non_finite_rate(client: TestClient) -> None:
    """An infinite rate can't be stored — it would bill every cell at it
    (NaN is already refused by ge=0). stdlib json.loads accepts the bare
    literal, so the value reaches the endpoint's finite check."""
    for field in (
        "input_per_mtok_usd",
        "output_per_mtok_usd",
        "cache_read_per_mtok_usd",
    ):
        resp = client.put(
            "/api/prices",
            content=f'{{"model_glob": "pr28-inf-*", "{field}": Infinity}}',
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422, f"{field}=Infinity must be rejected"


def test_put_rejects_local_with_nonzero_rate(client: TestClient) -> None:
    """local renders "local — $0" on every surface — a nonzero rate beside
    it is silent billing under a free label, so PUT refuses rather than
    coercing (zero/null rates stay valid, see the next test)."""
    for field in (
        "input_per_mtok_usd",
        "output_per_mtok_usd",
        "cache_read_per_mtok_usd",
    ):
        resp = _put(client, model_glob="pr28-localx-*", local=True, **{field: 5.0})
        assert resp.status_code == 422, f"local + {field}=5 must be rejected"
        assert "local" in resp.json()["detail"][0]["msg"]


def test_put_local_with_zero_or_null_rates_ok(
    client: TestClient, session_factory: sessionmaker[Session], cleanup_globs
) -> None:
    """Explicit zeros and an absent cache rate carry no lie — local + all
    zero/null rates upserts fine."""
    glob = "pr28-local-zero-*"
    cleanup_globs.append(glob)
    resp = _put(
        client,
        model_glob=glob,
        local=True,
        input_per_mtok_usd=0,
        output_per_mtok_usd=0.0,
        cache_read_per_mtok_usd=0,
    )
    assert resp.status_code == 200
    row = resp.json()
    assert row["local"] is True
    assert row["cache_read_per_mtok_usd"] == 0.0
    # omitted fields default to 0/None — equally valid beside local
    resp = _put(client, model_glob=glob, local=True)
    assert resp.status_code == 200
    assert resp.json()["cache_read_per_mtok_usd"] is None


def test_put_upserts_case_insensitive(
    client: TestClient, session_factory: sessionmaker[Session], cleanup_globs
) -> None:
    """Glob matching is case-insensitive, so a differently-cased PUT must
    update the SAME row — a second row would be shadowed by the first in
    declaration order and never take effect."""
    glob = "pr28-case-*"
    cleanup_globs.append(glob)
    first = _put(client, model_glob=glob, input_per_mtok_usd=1.0).json()
    second = _put(client, model_glob="PR28-CASE-*", input_per_mtok_usd=4.0).json()
    assert second["id"] == first["id"]
    assert second["input_per_mtok_usd"] == 4.0
    assert second["model_glob"] == glob  # stored glob keeps its casing
    globs = [r["model_glob"].lower() for r in client.get("/api/prices").json()["prices"]]
    assert globs.count(glob) == 1


def test_delete_price(
    client: TestClient, session_factory: sessionmaker[Session], cleanup_globs
) -> None:
    glob = "pr28-delete-me-*"
    cleanup_globs.append(glob)
    row = _put(client, model_glob=glob, output_per_mtok_usd=2.0).json()
    resp = client.delete(f"/api/prices/{row['id']}")
    assert resp.status_code == 204
    assert all(
        r["model_glob"] != glob for r in client.get("/api/prices").json()["prices"]
    )
    assert client.delete(f"/api/prices/{row['id']}").status_code == 404


def test_put_row_reprices_analytics(
    client: TestClient, session_factory: sessionmaker[Session], cleanup_globs
) -> None:
    """End-to-end layering: a PUT glob reprices a run's sandbox at the row's
    rate — the static table's own key no longer applies."""
    from db.models import Event, Run, Sandbox

    glob = "pr28-e2e-*"
    cleanup_globs.append(glob)
    _put(client, model_glob=glob, input_per_mtok_usd=10.0, output_per_mtok_usd=30.0)

    with session_factory() as s:
        run = Run(task_prompt="t", model_name="pr28-e2e-model", status="done")
        s.add(run)
        s.flush()
        sb = Sandbox(run_id=run.id, harness_config={"adapter": "miniloop"}, status="done")
        s.add(sb)
        s.flush()
        s.add(
            Event(
                run_id=run.id,
                sandbox_id=sb.id,
                source="gateway",
                type="llm_response",
                payload={
                    "model": "pr28-e2e-model",
                    "usage": {
                        "prompt_tokens": 1_000_000,
                        "completion_tokens": 100_000,
                        "total_tokens": 1_100_000,
                    },
                    "latency_ms": 100,
                },
            )
        )
        s.commit()
        run_id = run.id

    # auto mode: the run's model resolves through the book → the row's rate
    body = client.get(f"/api/runs/{run_id}/analytics").json()
    cell = body["sandboxes"][0]
    assert cell["price_model"] == glob
    # 1M in @ $10 + 100k out @ $30 → $10 + $3 = $13
    assert cell["estimated_cost_usd"] == pytest.approx(13.0)
    # the glob also lands in the response price_table for the picker
    assert body["price_table"][glob]["input_per_mtok_usd"] == 10.0
    # …and in price_globs (declaration order) for client-side key_for replay
    assert body["price_globs"][-1] == glob


def test_explicit_glob_pick_selects_its_row(
    client: TestClient, session_factory: sessionmaker[Session], cleanup_globs
) -> None:
    """?price_model=<glob> resolves to the picked row, not an earlier
    broader glob — the pick names the row itself on both analytics
    endpoints."""
    from db.models import Event, Run, Sandbox

    glob = "pr28-pick-*"
    _put(client, model_glob="pr28-*", input_per_mtok_usd=1.0, output_per_mtok_usd=1.0)
    _put(client, model_glob=glob, input_per_mtok_usd=10.0, output_per_mtok_usd=30.0)

    with session_factory() as s:
        run = Run(task_prompt="t", model_name="pr28-pick-model", status="done")
        s.add(run)
        s.flush()
        sb = Sandbox(run_id=run.id, harness_config={"adapter": "miniloop"}, status="done")
        s.add(sb)
        s.flush()
        s.add(
            Event(
                run_id=run.id,
                sandbox_id=sb.id,
                source="gateway",
                type="llm_response",
                payload={
                    "model": "pr28-pick-model",
                    "usage": {
                        "prompt_tokens": 1_000_000,
                        "completion_tokens": 100_000,
                        "total_tokens": 1_100_000,
                    },
                    "latency_ms": 100,
                },
            )
        )
        s.commit()
        run_id = run.id

    body = client.get(
        f"/api/runs/{run_id}/analytics", params={"price_model": glob}
    ).json()
    assert body["price_model"] == glob
    cell = body["sandboxes"][0]
    assert cell["price_model"] == glob
    # 1M in @ $10 + 100k out @ $30 → $10 + $3 = $13 at the PICKED row's
    # rate — the earlier "pr28-*" row ($1/$1) must not shadow it
    assert cell["estimated_cost_usd"] == pytest.approx(13.0)

    body = client.get("/api/analytics/runs", params={"price_model": glob}).json()
    assert body["price_model"] == glob


# ---------------------------------------------------------------------------
# POST /api/prices/sync — OpenRouter catalog
# ---------------------------------------------------------------------------


def _openrouter_catalog() -> dict[str, Any]:
    """OpenRouter /models shape: pricing values are USD-per-token strings."""
    return {
        "data": [
            {
                "id": "pr28-sync/alpha",
                "pricing": {
                    "prompt": "0.000001",  # $1/Mtok
                    "completion": "0.000002",  # $2/Mtok
                    "input_cache_read": "0.0000001",  # $0.10/Mtok
                },
            },
            {
                "id": "pr28-sync/beta",
                "pricing": {"prompt": "0.000005", "completion": "0.000015"},
            },
            {"id": "pr28-sync/free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "pr28-sync/no-pricing"},  # → skipped_unpriced
            {  # only one side priced → skipped (a half rate is a guess)
                "id": "pr28-sync/half",
                "pricing": {"prompt": "0.000001"},
            },
        ]
    }


@pytest.fixture()
def mock_gateway():
    """Inject a MockTransport as the api→gateway client for sync tests."""
    seen: list[httpx.Request] = []

    def dispatch(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=_openrouter_catalog())
        return httpx.Response(404)

    async def _dep() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(dispatch),
            base_url="http://gateway.test",
        ) as client:
            yield client

    app.dependency_overrides[get_gateway_client] = _dep
    yield seen
    app.dependency_overrides.pop(get_gateway_client, None)


def test_sync_requires_openrouter_provider(client: TestClient, mock_gateway) -> None:
    # tests pin UPSTREAM_PROVIDER=ollama — sync refuses before any HTTP call
    resp = client.post("/api/prices/sync")
    assert resp.status_code == 400
    assert "openrouter" in resp.json()["detail"]
    assert mock_gateway == []


def test_sync_maps_catalog_to_per_mtok(
    client: TestClient,
    session_factory: sessionmaker[Session],
    cleanup_globs,
    mock_gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "UPSTREAM_PROVIDER", "openrouter")
    for glob in ("pr28-sync/alpha", "pr28-sync/beta", "pr28-sync/free"):
        cleanup_globs.append(glob)

    # pre-existing USER row on the same id — sync must not clobber it
    user_glob = "pr28-sync/alpha"
    _put(
        client,
        model_glob=user_glob,
        input_per_mtok_usd=99.0,
        output_per_mtok_usd=99.0,
    )

    resp = client.post("/api/prices/sync")
    assert resp.status_code == 200
    body = resp.json()
    assert body["remote_count"] == 5
    # alpha collides with the user row → skipped; beta + free sync
    assert body["added"] == 2
    assert body["skipped_user"] == 1
    # no-pricing entry + half-priced entry (one side missing → a half
    # rate would be a guess, so it's skipped too)
    assert body["skipped_unpriced"] == 2

    rows = {r["model_glob"]: r for r in body["prices"]}
    beta = rows["pr28-sync/beta"]
    assert beta["source"] == "openrouter"
    # $0.000005/token prompt → $5.0/Mtok; $0.000015 → $15.0/Mtok
    assert beta["input_per_mtok_usd"] == pytest.approx(5.0)
    assert beta["output_per_mtok_usd"] == pytest.approx(15.0)
    assert beta["cache_read_per_mtok_usd"] is None
    free = rows["pr28-sync/free"]
    assert free["input_per_mtok_usd"] == 0.0
    # the user row kept its rate + source
    assert rows["pr28-sync/alpha"]["input_per_mtok_usd"] == 99.0
    assert rows["pr28-sync/alpha"]["source"] == "user"


def test_sync_updates_existing_openrouter_row(
    client: TestClient,
    session_factory: sessionmaker[Session],
    cleanup_globs,
    mock_gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "UPSTREAM_PROVIDER", "openrouter")
    glob = "pr28-sync/beta"
    cleanup_globs.append(glob)

    first = client.post("/api/prices/sync").json()
    assert first["added"] >= 1
    # second run: beta row exists with source=openrouter → updated, not added
    second = client.post("/api/prices/sync").json()
    assert second["updated"] >= 1
    betas = [
        r
        for r in client.get("/api/prices").json()["prices"]
        if r["model_glob"] == glob
    ]
    assert len(betas) == 1


def test_sync_maps_cache_read_rate(
    client: TestClient,
    session_factory: sessionmaker[Session],
    cleanup_globs,
    mock_gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """input_cache_read maps to cache_read_per_mtok_usd (×1e6)."""
    monkeypatch.setattr(get_settings(), "PROVIDER_ROUTES", json.dumps({"*": "openrouter"}))
    cleanup_globs.append("pr28-sync/alpha")

    body = client.post("/api/prices/sync").json()
    alpha = next(r for r in body["prices"] if r["model_glob"] == "pr28-sync/alpha")
    # 0.0000001 USD/token → 0.1 USD/Mtok
    assert alpha["cache_read_per_mtok_usd"] == pytest.approx(0.1)
    assert alpha["source"] == "openrouter"
    assert alpha["local"] is False
    # the request rode the gateway path with the service override header
    req = mock_gateway[0]
    assert req.headers.get("x-heg-provider") == "openrouter"
    assert req.headers.get("authorization", "").startswith("Bearer heg.")


def test_sync_case_variant_id_and_non_finite_rates(
    client: TestClient,
    session_factory: sessionmaker[Session],
    cleanup_globs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catalog id differing only in case still collides with the existing
    row (glob matching is case-insensitive — a second row would be a
    shadowed duplicate), and non-finite catalog rates are skipped like any
    other unparseable pricing."""
    monkeypatch.setattr(get_settings(), "UPSTREAM_PROVIDER", "openrouter")
    cleanup_globs.append("pr28-sync/casey")
    _put(client, model_glob="pr28-sync/casey", input_per_mtok_usd=7.0)

    def dispatch(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "PR28-SYNC/CASEY",
                        "pricing": {"prompt": "0.000003", "completion": "0.000004"},
                    },
                    {
                        "id": "pr28-sync/nan",
                        "pricing": {"prompt": "nan", "completion": "nan"},
                    },
                ]
            },
        )

    async def _dep() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(dispatch), base_url="http://gateway.test"
        ) as c:
            yield c

    app.dependency_overrides[get_gateway_client] = _dep
    try:
        body = client.post("/api/prices/sync").json()
    finally:
        app.dependency_overrides.pop(get_gateway_client, None)

    # the case-variant id hit the existing user row — protected, not duplicated
    assert body["skipped_user"] == 1
    assert body["added"] == 0
    # a NaN rate is as unusable as a missing one — skipped, never persisted
    assert body["skipped_unpriced"] == 1
    rows = {r["model_glob"]: r for r in body["prices"]}
    assert rows["pr28-sync/casey"]["input_per_mtok_usd"] == 7.0
    assert "PR28-SYNC/CASEY" not in rows
    assert "pr28-sync/nan" not in rows


def test_sync_skips_oversized_model_id(
    client: TestClient,
    session_factory: sessionmaker[Session],
    cleanup_globs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A catalog id longer than ``model_glob``'s String(255) can't persist
    — it must be skipped like a malformed entry, not fail the commit and
    lose the whole sync."""
    monkeypatch.setattr(get_settings(), "UPSTREAM_PROVIDER", "openrouter")
    cleanup_globs.append("pr28-sync/fits")

    def dispatch(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "pr28-sync/" + "x" * 300,
                        "pricing": {"prompt": "0.000001", "completion": "0.000001"},
                    },
                    {
                        "id": "pr28-sync/fits",
                        "pricing": {"prompt": "0.000001", "completion": "0.000001"},
                    },
                ]
            },
        )

    async def _dep() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(dispatch), base_url="http://gateway.test"
        ) as c:
            yield c

    app.dependency_overrides[get_gateway_client] = _dep
    try:
        resp = client.post("/api/prices/sync")
    finally:
        app.dependency_overrides.pop(get_gateway_client, None)

    assert resp.status_code == 200
    body = resp.json()
    # the oversized id was skipped; the valid sibling still persisted
    assert body["added"] == 1
    globs = [r["model_glob"] for r in body["prices"]]
    assert "pr28-sync/fits" in globs
    assert not any(len(g) > 255 for g in globs)
