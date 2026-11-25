"""PR32 — cross-run leaderboard tests (GET /api/analytics/leaderboard).

The endpoint aggregates across EVERY run in the DB, so unlike the per-run
analytics tests it can't ride the shared in-memory fixture — rows from
other tests in the process would leak into the boards. Same isolation
pattern as test_presets.py: a tmp_path sqlite file per test +
``app.dependency_overrides[get_db]``.

Score merge rule under test: asc (created_at, seq, id) → newest ``overall``
wins per sandbox.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from api.deps import get_db
from api.main import app
from db.models import Base, Run, Sandbox, Score
from db.session import make_engine, make_session_factory

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def api(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path}/leaderboard.db")
    Base.metadata.create_all(engine)
    sf = make_session_factory(engine)

    def override_get_db():
        session = sf()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    yield SimpleNamespace(client=TestClient(app), session_factory=sf)
    app.dependency_overrides.pop(get_db, None)
    engine.dispose()


def _score(sb_id, value, *, ts=T0, seq=None) -> Score:
    return Score(
        sandbox_id=sb_id,
        dimension="overall",
        score=value,
        rationale="",
        created_at=ts,
        seq=seq,
    )


@pytest.fixture()
def leaderboard_data(api):
    """Two runs on preset ``p2048`` + one on ``snake`` + one untrialed.

    run_a (preset p2048, trials=2, model qwen3:8b):
      - sb_a1 miniloop+superpowers (named "sp") → overall 8.0, rescored → 9.0
      - sb_a2 miniloop+superpowers (named "sp") → 7.0   (trials of one cell)
      - sb_a3 miniloop bare on gpt-4o pin      → 6.0
    run_b (preset p2048, trials=1, model gpt-4o):
      - sb_b1 miniloop+superpowers             → 4.0
      - sb_b2 codex bare                       → unscored
    run_c (preset snake, trials=3, model qwen3:8b):
      - sb_c1 aider+spec-kit                   → 5.0
    run_d (no preset, trials=1, model qwen3:8b):
      - sb_d1 miniloop bare                    → 2.0
    """
    s: Session = api.session_factory()
    try:
        run_a = Run(
            task_prompt="2048",
            model_name="qwen3:8b",
            trials=2,
            preset_ref="p2048",
            config={},
            status="done",
            created_at=T0,
        )
        run_b = Run(
            task_prompt="2048 again",
            model_name="gpt-4o",
            trials=1,
            preset_ref="p2048",
            config={},
            status="done",
            created_at=T0 + timedelta(days=1),
        )
        run_c = Run(
            task_prompt="snake",
            model_name="qwen3:8b",
            trials=3,
            preset_ref="snake",
            config={},
            status="done",
            created_at=T0 + timedelta(days=2),
        )
        run_d = Run(
            task_prompt="freeform",
            model_name="qwen3:8b",
            trials=1,
            config={},
            status="done",
            created_at=T0 + timedelta(days=3),
        )
        s.add_all([run_a, run_b, run_c, run_d])
        s.flush()

        sb_a1 = Sandbox(
            run_id=run_a.id,
            name="sp",
            harness_config={
                "adapter": "miniloop",
                "skill_packs": ["superpowers"],
                "name": "sp",
                "trial_index": 1,
            },
            status="done",
            terminal_reason="agent_done",
        )
        sb_a2 = Sandbox(
            run_id=run_a.id,
            name="sp",
            harness_config={
                "adapter": "miniloop",
                "skill_packs": ["superpowers"],
                "name": "sp",
                "trial_index": 2,
            },
            status="done",
            terminal_reason="agent_done",
        )
        sb_a3 = Sandbox(
            run_id=run_a.id,
            harness_config={
                "adapter": "miniloop",
                "skill_packs": [],
                "model": "gpt-4o",
            },
            status="done",
            terminal_reason="agent_done",
        )
        sb_b1 = Sandbox(
            run_id=run_b.id,
            harness_config={
                "adapter": "miniloop",
                "skill_packs": ["superpowers"],
                "model": "gpt-4o",
            },
            status="done",
            terminal_reason="timeout",
        )
        sb_b2 = Sandbox(
            run_id=run_b.id,
            harness_config={"adapter": "codex", "skill_packs": []},
            status="failed",
            terminal_reason="error",
        )
        sb_c1 = Sandbox(
            run_id=run_c.id,
            harness_config={"adapter": "aider", "skill_packs": ["spec-kit"]},
            status="done",
            terminal_reason="agent_done",
        )
        sb_d1 = Sandbox(
            run_id=run_d.id,
            harness_config={"adapter": "miniloop", "skill_packs": []},
            status="done",
            terminal_reason="agent_done",
        )
        s.add_all([sb_a1, sb_a2, sb_a3, sb_b1, sb_b2, sb_c1, sb_d1])
        s.flush()
        s.add_all(
            [
                # sb_a1: v1 score then a rescore — newest (seq) must win.
                _score(sb_a1.id, 8.0, ts=T0 + timedelta(hours=1), seq=1),
                _score(sb_a1.id, 9.0, ts=T0 + timedelta(hours=2), seq=2),
                _score(sb_a2.id, 7.0, ts=T0 + timedelta(hours=1), seq=1),
                _score(sb_a3.id, 6.0, ts=T0 + timedelta(hours=1), seq=1),
                _score(sb_b1.id, 4.0, ts=T0 + timedelta(days=1, hours=1), seq=1),
                # sb_b2: no scores at all — unscored cell.
                _score(sb_c1.id, 5.0, ts=T0 + timedelta(days=2, hours=1), seq=1),
                _score(sb_d1.id, 2.0, ts=T0 + timedelta(days=3, hours=1), seq=1),
            ]
        )
        s.commit()
        return {
            "runs": {"a": run_a.id, "b": run_b.id, "c": run_c.id, "d": run_d.id},
            "sandboxes": {
                "a1": sb_a1.id,
                "a2": sb_a2.id,
                "a3": sb_a3.id,
                "b1": sb_b1.id,
                "b2": sb_b2.id,
                "c1": sb_c1.id,
                "d1": sb_d1.id,
            },
        }
    finally:
        s.close()


def _get(client: TestClient, **params) -> dict:
    resp = client.get("/api/analytics/leaderboard", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_leaderboard_ranks_models(api, leaderboard_data) -> None:
    body = _get(api.client)
    assert body["runs_count"] == 4
    assert body["sandboxes_count"] == 7
    assert body["n_scored"] == 6  # sb_b2 unscored

    by_model = {e["key"]: e for e in body["by_model"]}
    # qwen3:8b cells: a1(9.0 rescore wins), a2(7.0), c1(5.0), d1(2.0)
    q = by_model["qwen3:8b"]
    assert q["label"] == "qwen3:8b"
    assert q["n"] == 4
    assert q["runs"] == 3
    assert q["sandboxes"] == 4
    assert q["mean_overall_score"] == pytest.approx(5.75)
    # sample sd (n−1) of {9,7,5,2} around 5.75 → 2.986 (rounded to 3dp)
    expected_sd = (sum((x - 5.75) ** 2 for x in (9.0, 7.0, 5.0, 2.0)) / 3) ** 0.5
    assert q["sd_overall_score"] == pytest.approx(round(expected_sd, 3))
    # gpt-4o cells: a3(6.0) + b1(4.0) → mean 5.0, sd=√2→1.414, n=2, runs=2
    g = by_model["gpt-4o"]
    assert g["n"] == 2
    assert g["runs"] == 2
    assert g["mean_overall_score"] == pytest.approx(5.0)
    assert g["sd_overall_score"] == pytest.approx(1.414)
    # codex sandbox scored nothing → no codex row on either board.
    assert all(e["key"] != "codex" for e in body["by_model"])
    assert all(e["adapter"] != "codex" for e in body["by_config"])
    # Rank order: qwen (5.75) > gpt-4o (5.0)
    assert [e["key"] for e in body["by_model"]] == ["qwen3:8b", "gpt-4o"]


def test_leaderboard_config_families(api, leaderboard_data) -> None:
    body = _get(api.client)
    families = {e["key"]: e for e in body["by_config"]}
    # miniloop+superpowers family pools a1+a2 (name "sp") + b1 →
    # scores 9/7/4 → mean 20/3 → 6.67, n=3, runs=2, sandboxes=3;
    # one distinct non-empty member name surfaces on the entry.
    fam = families["family:miniloop|superpowers"]
    assert fam["label"] == "miniloop · superpowers"
    assert fam["adapter"] == "miniloop"
    assert fam["name"] == "sp"
    assert fam["n"] == 3
    assert fam["runs"] == 2
    assert fam["sandboxes"] == 3
    assert fam["mean_overall_score"] == pytest.approx(6.67)
    # model axis excluded from the family — a3/d1 land in the bare-miniloop
    # family regardless of which model each ran.
    bare = families["family:miniloop|"]
    assert bare["n"] == 2  # a3(6.0) + d1(2.0) — model differs, family same
    assert bare["runs"] == 2
    assert bare["name"] is None
    aider = families["family:aider|spec-kit"]
    assert aider["n"] == 1
    assert aider["sd_overall_score"] is None  # n<2 → no spread reported
    # family board order: 6.67 > 5.0 > 4.0
    assert [e["key"] for e in body["by_config"]] == [
        "family:miniloop|superpowers",
        "family:aider|spec-kit",
        "family:miniloop|",
    ]


def test_leaderboard_preset_filter(api, leaderboard_data) -> None:
    body = _get(api.client, preset="p2048")
    assert body["preset"] == "p2048"
    assert body["runs_count"] == 2
    assert body["sandboxes_count"] == 5
    assert body["n_scored"] == 4
    by_model = {e["key"]: e for e in body["by_model"]}
    # qwen only a1(9)+a2(7) → mean 8.0; gpt-4o a3(6)+b1(4) → 5.0
    assert by_model["qwen3:8b"]["mean_overall_score"] == pytest.approx(8.0)
    assert by_model["qwen3:8b"]["runs"] == 1
    assert by_model["gpt-4o"]["mean_overall_score"] == pytest.approx(5.0)
    assert by_model["gpt-4o"]["runs"] == 2
    # an unknown preset → empty board, not an error.
    empty = _get(api.client, preset="nope")
    assert empty["runs_count"] == 0
    assert empty["by_model"] == []


def test_leaderboard_date_range_filter(api, leaderboard_data) -> None:
    # runs: a @ T0, b @ +1d, c @ +2d, d @ +3d — keep only b+c.
    body = _get(
        api.client,
        since=(T0 + timedelta(days=1)).isoformat(),
        until=(T0 + timedelta(days=2, hours=12)).isoformat(),
    )
    assert body["runs_count"] == 2
    assert body["n_scored"] == 2  # b1 + c1
    by_model = {e["key"]: e for e in body["by_model"]}
    # b contributes gpt-4o(4.0), c contributes qwen(5.0)
    assert by_model["gpt-4o"]["mean_overall_score"] == pytest.approx(4.0)
    assert by_model["qwen3:8b"]["mean_overall_score"] == pytest.approx(5.0)


def test_leaderboard_min_trials_filter(api, leaderboard_data) -> None:
    # min_trials=2 keeps runs a(2) + c(3); b(1) and d(1) drop out.
    body = _get(api.client, min_trials=2)
    assert body["min_trials"] == 2
    assert body["runs_count"] == 2
    assert body["n_scored"] == 4  # a1,a2,a3,c1
    by_model = {e["key"]: e for e in body["by_model"]}
    assert by_model["qwen3:8b"]["mean_overall_score"] == pytest.approx(
        round((9.0 + 7.0 + 5.0) / 3, 2)
    )
    assert by_model["qwen3:8b"]["n"] == 3
    assert by_model["gpt-4o"]["n"] == 1
    assert by_model["gpt-4o"]["sd_overall_score"] is None


def test_leaderboard_empty_scope(api) -> None:
    body = _get(api.client)
    assert body["runs_count"] == 0
    assert body["sandboxes_count"] == 0
    assert body["n_scored"] == 0
    assert body["by_model"] == []
    assert body["by_config"] == []


def test_leaderboard_bad_range_422(api) -> None:
    resp = api.client.get(
        "/api/analytics/leaderboard",
        params={
            "since": "2026-02-01T00:00:00",
            "until": "2026-01-01T00:00:00",
        },
    )
    assert resp.status_code == 422


def test_leaderboard_bad_range_mixed_tz_422(api) -> None:
    """The UI's date filter sends a bare date ``since`` (parses naive)
    with an end-of-day ``Z`` ``until`` (parses aware) — the bounds check
    must normalize tz before comparing, not 500 on a naive/aware ``>``."""
    resp = api.client.get(
        "/api/analytics/leaderboard",
        params={"since": "2026-02-01", "until": "2026-01-01T23:59:59.999Z"},
    )
    assert resp.status_code == 422


def test_leaderboard_mixed_tz_range_ok(api, leaderboard_data) -> None:
    """The valid-direction version of the UI's shipped filter shape —
    naive date ``since`` + aware end-of-day ``until`` — filters, not 500s."""
    body = _get(
        api.client,
        since=(T0 + timedelta(days=1)).date().isoformat(),
        until=f"{(T0 + timedelta(days=2)).date().isoformat()}T23:59:59.999Z",
    )
    assert body["runs_count"] == 2  # run_b + run_c
    assert body["n_scored"] == 2


def test_leaderboard_naive_since_treated_as_utc(api, leaderboard_data) -> None:
    """A date-only ``?since=`` (HTML date input) parses naive — the filter
    must still bind honestly against the timestamptz column."""
    body = _get(api.client, since=(T0 + timedelta(days=3)).date().isoformat())
    assert body["runs_count"] == 1  # only run_d survives
    assert body["n_scored"] == 1
