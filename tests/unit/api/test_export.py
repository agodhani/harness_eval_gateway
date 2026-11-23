"""PR35 (F28) — run export + share-link API tests.

``GET /api/runs/{id}/export?fmt=json|csv`` — the JSON bundle round-trips
through :class:`api.routers.results.RunExport` (the re-import contract);
the CSV flattens to one row per sandbox × score dimension.

``POST/DELETE /api/runs/{id}/share`` + ``GET /api/share/{token}`` — the
read-only share link surface (stretch half of the card).
"""

from __future__ import annotations

import csv
import io
import uuid

import pytest
from sqlalchemy.orm import Session, sessionmaker

from api.deps import get_session_factory
from api.routers.results import RunExport, ShareOut
from db.models import Base, Event, Rubric, Run, Sandbox, Score


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    sf = get_session_factory()
    Base.metadata.create_all(sf.kw["bind"])
    return sf


@pytest.fixture()
def scored_run(session_factory: sessionmaker[Session]) -> dict:
    """A done run: 2 sandboxes (one scored, one not), 1 rubric, events."""
    s = session_factory()
    try:
        run = Run(
            task_prompt="build 2048",
            model_name="qwen3:8b",
            config={
                "evaluator_model": "qwen3:8b",
                "same_model_eval": True,
                "harness_configs": [{"adapter": "miniloop"}],
            },
            status="done",
        )
        s.add(run)
        s.flush()
        sb1 = Sandbox(
            run_id=run.id,
            name="cfg-a",
            harness_config={"adapter": "miniloop", "skill_packs": ["superpowers"]},
            status="done",
            terminal_reason="agent_done",
        )
        sb2 = Sandbox(
            run_id=run.id,
            name="cfg-b",
            harness_config={"adapter": "miniloop", "skill_packs": []},
            status="failed",
            terminal_reason="timeout",
        )
        s.add_all([sb1, sb2])
        s.flush()
        rubric = Rubric(
            run_id=run.id,
            version=1,
            status="approved",
            content={"dimensions": [{"name": "functional_requirements"}]},
        )
        s.add(rubric)
        s.flush()
        s.add_all(
            [
                Score(
                    sandbox_id=sb1.id,
                    rubric_id=rubric.id,
                    rubric_version=1,
                    dimension="functional_requirements",
                    score=8.0,
                    rationale="game loop works",
                ),
                Score(
                    sandbox_id=sb1.id,
                    rubric_id=rubric.id,
                    rubric_version=1,
                    dimension="code_quality",
                    score=6.0,
                    rationale="readable",
                ),
                Score(
                    sandbox_id=sb1.id,
                    rubric_id=rubric.id,
                    rubric_version=1,
                    dimension="overall",
                    score=7.0,
                    rationale="solid attempt",
                ),
            ]
        )
        s.add_all(
            [
                Event(
                    run_id=run.id,
                    sandbox_id=sb1.id,
                    source="harness",
                    type="llm_response",
                    payload={
                        "model": "qwen3:8b",
                        "latency_ms": 812,
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 20,
                            "total_tokens": 30,
                        },
                        # nested non-usage content — must be dropped by the
                        # events-lite projection
                        "messages": [{"role": "user", "content": "x" * 5000}],
                    },
                ),
                Event(
                    run_id=run.id,
                    sandbox_id=None,
                    source="orchestrator",
                    type="run_started",
                    payload={"models": ["qwen3:8b"], "note": "go"},
                ),
            ]
        )
        s.commit()
        return {"run_id": run.id, "sb1": sb1.id, "sb2": sb2.id}
    finally:
        s.close()


# ---------------------------------------------------------------------------
# GET /api/runs/{run_id}/export?fmt=json
# ---------------------------------------------------------------------------


def test_export_json_roundtrip(client, scored_run) -> None:
    """The JSON bundle downloads as an attachment AND validates against the
    export schema — the re-import contract."""
    run_id = scored_run["run_id"]
    resp = client.get(f"/api/runs/{run_id}/export?fmt=json")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    cd = resp.headers["content-disposition"]
    assert "attachment" in cd and f"heg-run-{run_id}.json" in cd

    bundle = RunExport.model_validate(resp.json())
    assert bundle.format == "heg-run-export"
    assert bundle.format_version == 1
    # spec
    assert bundle.spec.id == run_id
    assert bundle.spec.task_prompt == "build 2048"
    assert bundle.spec.models == ["qwen3:8b"]
    assert bundle.spec.config["harness_configs"] == [{"adapter": "miniloop"}]
    # rubric versions incl. content
    assert len(bundle.rubrics) == 1
    assert bundle.rubrics[0].content["dimensions"] == [
        {"name": "functional_requirements"}
    ]
    # score matrix — same shape the results page reads
    assert len(bundle.results.sandboxes) == 2
    sb1 = next(
        s for s in bundle.results.sandboxes
        if s.sandbox_id == scored_run["sb1"]
    )
    assert sb1.dimensions["functional_requirements"].score == 8.0
    assert sb1.overall is not None and sb1.overall.score == 7.0
    # analytics — same shape the analysis page reads
    assert {s.sandbox_id for s in bundle.analytics.sandboxes} == {
        scored_run["sb1"],
        scored_run["sb2"],
    }
    # events-lite: 2 rows, payload projected (below)
    assert bundle.events_total == 2
    assert bundle.events_truncated is False
    assert len(bundle.events) == 2


def test_export_json_events_lite_projection(client, scored_run) -> None:
    """Events-lite keeps scalar fields + the usage dict; nested content is
    dropped and long strings capped — never a transcript."""
    resp = client.get(f"/api/runs/{scored_run['run_id']}/export")
    ev = next(e for e in resp.json()["events"] if e["type"] == "llm_response")
    assert ev["payload"]["model"] == "qwen3:8b"
    assert ev["payload"]["latency_ms"] == 812
    assert ev["payload"]["usage"]["total_tokens"] == 30
    assert "messages" not in ev["payload"]

    started = next(e for e in resp.json()["events"] if e["type"] == "run_started")
    # nested non-usage dict dropped, scalar kept
    assert "models" not in started["payload"]
    assert started["payload"]["note"] == "go"


def test_export_json_404(client) -> None:
    resp = client.get(f"/api/runs/{uuid.uuid4()}/export")
    assert resp.status_code == 404


def test_export_bad_fmt(client, scored_run) -> None:
    resp = client.get(f"/api/runs/{scored_run['run_id']}/export?fmt=yaml")
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/runs/{run_id}/export?fmt=csv
# ---------------------------------------------------------------------------


def _csv_rows(body: str) -> list[dict[str, str]]:
    """Parse the exported sheet, tolerating the UTF-8 BOM."""
    text = body.lstrip("\ufeff")
    return list(csv.DictReader(io.StringIO(text)))


def test_export_csv(client, scored_run) -> None:
    """One row per sandbox×dimension — scored sandbox contributes its dims
    + an ``overall`` row; the unscored sandbox still contributes one row so
    the sheet covers the full matrix."""
    run_id = scored_run["run_id"]
    resp = client.get(f"/api/runs/{run_id}/export?fmt=csv")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.text.startswith("\ufeff")  # Excel-friendly UTF-8 BOM

    rows = _csv_rows(resp.text)
    # sb1: functional_requirements + code_quality + overall = 3 rows;
    # sb2: 1 score-less row.
    assert len(rows) == 4
    by_dim = {(r["sandbox_id"], r["dimension"]): r for r in rows}
    fr = by_dim[(str(scored_run["sb1"]), "functional_requirements")]
    assert fr["score"] == "8.0"
    assert fr["rationale"] == "game loop works"
    assert fr["model"] == "qwen3:8b"
    assert fr["adapter"] == "miniloop"
    assert fr["sandbox_name"] == "cfg-a"
    assert fr["rubric_version"] == "1"
    ov = by_dim[(str(scored_run["sb1"]), "overall")]
    assert ov["score"] == "7.0"
    # analytics columns repeat per dimension row — sb1's one metered
    # llm_response contributes its turn + usage tokens to every row.
    assert ov["llm_turns"] == "1"
    assert ov["tokens_total"] == "30"
    # unscored sandbox → single row, empty dimension/score
    blank = [r for r in rows if r["sandbox_id"] == str(scored_run["sb2"])]
    assert len(blank) == 1
    assert blank[0]["dimension"] == "" and blank[0]["score"] == ""
    assert blank[0]["status"] == "failed"
    assert blank[0]["terminal_reason"] == "timeout"


def test_export_csv_404(client) -> None:
    resp = client.get(f"/api/runs/{uuid.uuid4()}/export?fmt=csv")
    assert resp.status_code == 404


def test_export_csv_empty_run(client, session_factory) -> None:
    """A run with no sandboxes exports a header-only sheet."""
    s = session_factory()
    run = Run(task_prompt="t", model_name="m", status="submitted")
    s.add(run)
    s.commit()
    s.close()
    resp = client.get(f"/api/runs/{run.id}/export?fmt=csv")
    assert resp.status_code == 200
    rows = _csv_rows(resp.text)
    assert rows == []
    assert "sandbox_id" in resp.text  # header still present


def test_export_csv_legacy_harness_key(client, session_factory) -> None:
    """A sandbox carrying the legacy ``harness`` key exports its adapter
    name — same fallback the matrix label + analytics apply."""
    s = session_factory()
    run = Run(task_prompt="t", model_name="m", status="done")
    s.add(run)
    s.flush()
    s.add(
        Sandbox(
            run_id=run.id,
            name="cfg-x",
            harness_config={"harness": "codex"},
            status="done",
        )
    )
    s.commit()
    s.close()
    resp = client.get(f"/api/runs/{run.id}/export?fmt=csv")
    assert resp.status_code == 200
    rows = _csv_rows(resp.text)
    assert len(rows) == 1
    assert rows[0]["adapter"] == "codex"


def test_export_json_empty_run(client, session_factory) -> None:
    """A run with no sandboxes/events still validates + downloads."""
    s = session_factory()
    run = Run(task_prompt="t", model_name="m", status="submitted")
    s.add(run)
    s.commit()
    s.close()
    resp = client.get(f"/api/runs/{run.id}/export")
    assert resp.status_code == 200
    bundle = RunExport.model_validate(resp.json())
    assert bundle.results.sandboxes == []
    assert bundle.rubrics == []
    assert bundle.events == []
    assert bundle.events_total == 0


# ---------------------------------------------------------------------------
# share links — POST/DELETE /api/runs/{id}/share + GET /api/share/{token}
# ---------------------------------------------------------------------------


def test_share_mint_get_revoke(client, scored_run) -> None:
    run_id = scored_run["run_id"]
    resp = client.post(f"/api/runs/{run_id}/share")
    assert resp.status_code == 200
    body = resp.json()
    token = body["token"]
    assert len(token) >= 16
    assert body["url"] == f"/share/{token}"

    # idempotent — second POST returns the same token
    again = client.post(f"/api/runs/{run_id}/share")
    assert again.status_code == 200
    assert again.json()["token"] == token

    # the run detail now advertises the share path
    detail = client.get(f"/api/runs/{run_id}")
    assert detail.json()["share_url"] == f"/share/{token}"

    # the read-only bundle resolves
    shared = client.get(f"/api/share/{token}")
    assert shared.status_code == 200
    out = ShareOut.model_validate(shared.json())
    assert out.run.id == run_id
    assert out.run.task_prompt == "build 2048"
    assert len(out.results.sandboxes) == 2
    assert out.events_total == 2
    # events-lite in the share view — same projection as the export
    assert all("messages" not in e.payload for e in out.events)

    # revoke → the token stops resolving (identical 404 to unknown)
    assert client.delete(f"/api/runs/{run_id}/share").status_code == 204
    assert client.get(f"/api/share/{token}").status_code == 404
    # idempotent revoke + the run detail clears the link
    assert client.delete(f"/api/runs/{run_id}/share").status_code == 204
    assert client.get(f"/api/runs/{run_id}").json()["share_url"] is None


def test_share_mint_unknown_run_404(client) -> None:
    assert client.post(f"/api/runs/{uuid.uuid4()}/share").status_code == 404
    assert client.delete(f"/api/runs/{uuid.uuid4()}/share").status_code == 404


def test_share_unknown_token_404(client) -> None:
    assert client.get("/api/share/does-not-exist").status_code == 404


def test_share_events_bounded(client, scored_run, session_factory) -> None:
    """events_total reports the true stream length; the rows list is the
    bounded lite projection."""
    s = session_factory()
    try:
        for i in range(5):
            s.add(
                Event(
                    run_id=scored_run["run_id"],
                    sandbox_id=scored_run["sb1"],
                    source="harness",
                    type="shell_command",
                    payload={"command": f"echo {i}", "exit_code": 0},
                )
            )
        s.commit()
    finally:
        s.close()
    client.post(f"/api/runs/{scored_run['run_id']}/share")
    token = client.get(f"/api/runs/{scored_run['run_id']}").json()[
        "share_url"
    ].rsplit("/", 1)[-1]
    out = ShareOut.model_validate(client.get(f"/api/share/{token}").json())
    assert out.events_total == 7
    assert len(out.events) == 7  # under the cap — nothing truncated
    assert out.events_truncated is False
    shell = [e for e in out.events if e.type == "shell_command"]
    assert shell[0].payload["command"] == "echo 0"
    assert shell[0].payload["exit_code"] == 0
