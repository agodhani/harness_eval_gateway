"""evaluate_sandbox tests with a mocked SandboxRuntime (F7).

Hermetic: no docker, no LLM. The fake runtime's ``spawn`` writes a canned
scores.json into the staged workspace (simulating the judge agent), and
``status`` reports an immediate exit — the same contract the real runtime
exposes (spawn/status/stop/remove).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base, Event, Rubric, Run, Sandbox, Score
from evaluator.evaluate import evaluate_sandbox
from evaluator.sandbox_spec import eval_sandbox_id, sandbox_gateway_url
from sandbox.runtime import SandboxRuntime, SpawnResult

RUBRIC = {
    "summary": "Say hi.",
    "dimensions": [
        {"name": "functional_requirements", "score": 9, "rationale": "works"},
        {"name": "code_quality", "score": 5, "rationale": "trivial"},
    ],
}

SCORES_OBJ = {
    "scores": [
        {"dimension": "functional_requirements", "score": 8, "rationale": "hello.txt present"},
        {"dimension": "code_quality", "score": 6, "rationale": "one file"},
    ],
    "overall": 7.0,
    "summary": "Minimal greeting program.",
}


class FakeRuntime:
    """Duck-typed SandboxRuntime: spawn writes canned scores.json."""

    def __init__(self, scores_obj: dict | None = SCORES_OBJ, status: str = "exited") -> None:
        self.scores_obj = scores_obj
        self.status_value = status
        self.specs: list = []
        self.spawned: list[str] = []
        self.stopped: list[str] = []
        self.removed: list[str] = []

    def spawn(self, spec) -> SpawnResult:
        self.specs.append(spec)
        self.spawned.append(spec.sandbox_id)
        if self.scores_obj is not None:
            ws = Path(spec.workspace_host_path)
            (ws / "scores.json").write_text(json.dumps(self.scores_obj))
        return SpawnResult(
            sandbox_id=spec.sandbox_id,
            run_id=spec.run_id,
            container_id="fakecontainerid",
            container_name=SandboxRuntime.container_name(spec.sandbox_id),
            image=spec.image,
        )

    def status(self, sandbox_id: str) -> dict:
        return {
            "sandbox_id": sandbox_id,
            "status": self.status_value,
            "exit_code": 0 if self.status_value == "exited" else None,
            "running": self.status_value == "running",
        }

    def stop(self, sandbox_id: str, *, remove: bool = True, timeout: int = 10) -> None:
        self.stopped.append(sandbox_id)
        self.status_value = "exited"

    def remove(self, sandbox_id: str) -> None:
        self.removed.append(sandbox_id)


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def seeded(session_factory: sessionmaker[Session], tmp_path: Path):
    """Run + Sandbox + approved Rubric + artifact workspace on disk."""
    artifact_dir = tmp_path / "artifact"
    ws = artifact_dir / "workspace"
    ws.mkdir(parents=True)
    (ws / "hello.txt").write_text("hello\n")
    (ws / "main.py").write_text("print('hi')\n")
    transcript = artifact_dir / "transcript.log"
    transcript.write_text("MINILOOP_RESULT {}\n")

    s = session_factory()
    try:
        run = Run(task_prompt="say hi", model_name="participant-model", config={})
        s.add(run)
        s.flush()
        sb = Sandbox(
            run_id=run.id,
            harness_config={"adapter": "miniloop", "skill_packs": []},
            status="done",
            terminal_reason="agent_done",
            artifact_path=str(artifact_dir),
        )
        s.add(sb)
        s.flush()
        rubric = Rubric(run_id=run.id, version=1, status="approved", content=RUBRIC)
        s.add(rubric)
        s.commit()
        ids = {"run_id": run.id, "sandbox_id": sb.id, "rubric_id": rubric.id}
    finally:
        s.close()
    return {
        **ids,
        "artifact_dir": artifact_dir,
        "workspace": ws,
        "transcript": transcript,
        "eval_root": tmp_path / "eval",
    }


async def _eval(seeded, sf, runtime, **overrides):
    kwargs = {
        "run_id": seeded["run_id"],
        "sandbox_id": seeded["sandbox_id"],
        "rubric_id": seeded["rubric_id"],
        "rubric": RUBRIC,
        "artifact_dir": seeded["artifact_dir"],
        "workspace_dir": seeded["workspace"],
        "transcript_path": seeded["transcript"],
        "terminal_reason": "agent_done",
        "model": "judge-model",
        "gateway_cred": "heg.testcred",
        "session_factory": sf,
        "runtime": runtime,
        "eval_root": seeded["eval_root"],
        "timeout_seconds": 30,
        "poll_interval_seconds": 0.01,
        "max_iters": 5,
    }
    kwargs.update(overrides)
    return await evaluate_sandbox(**kwargs)


def _scores_in_db(session_factory) -> list[Score]:
    with session_factory() as s:
        return list(s.execute(select(Score)).scalars().all())


def _events_in_db(session_factory) -> list[Event]:
    with session_factory() as s:
        return list(s.execute(select(Event)).scalars().all())


async def test_evaluate_happy_path(seeded, session_factory) -> None:
    fake = FakeRuntime()
    rows = await _eval(seeded, session_factory, fake)

    # 2 dimensions + 1 overall pseudo-row
    assert len(rows) == 3
    dims = {r.dimension: r for r in rows}
    assert dims["functional_requirements"].score == 8.0
    assert dims["code_quality"].score == 6.0
    assert dims["overall"].score == 7.0
    assert dims["overall"].rationale == "Minimal greeting program."
    assert all(r.sandbox_id == seeded["sandbox_id"] for r in rows)
    assert all(r.rubric_id == seeded["rubric_id"] for r in rows)

    # persisted
    db_rows = _scores_in_db(session_factory)
    assert len(db_rows) == 3

    # eval sandbox spawned + removed; env contract honoured
    eval_sid = eval_sandbox_id(seeded["sandbox_id"])
    assert fake.spawned == [eval_sid]
    assert fake.removed == [eval_sid]
    spec = fake.specs[0]
    assert spec.sandbox_id == eval_sid
    assert spec.run_id == str(seeded["run_id"])
    assert spec.env["GATEWAY_CRED"] == "heg.testcred"
    assert spec.env["MODEL_NAME"] == "judge-model"
    assert spec.env["MAX_ITERS"] == "5"
    assert spec.env["GATEWAY_BASE_URL"] == "http://gateway:8080"
    assert spec.env["EVENTS_INGEST_URL"] == "http://gateway:8080/internal/events"
    assert "EVAL_PROMPT.md" in spec.env["TASK_PROMPT"]

    # staged workspace contents
    eval_ws = Path(spec.workspace_host_path)
    assert (eval_ws / "hello.txt").read_text() == "hello\n"
    assert (eval_ws / "main.py").exists()
    assert (eval_ws / "EVAL_TRANSCRIPT.log").exists()
    assert json.loads((eval_ws / "rubric.json").read_text())["summary"] == "Say hi."
    assert "functional_requirements" in (eval_ws / "EVAL_PROMPT.md").read_text()
    assert json.loads((eval_ws / "scores.json").read_text())["overall"] == 7.0

    # evaluator events: eval_started + eval_scored checkpoints
    events = _events_in_db(session_factory)
    phases = [e.payload.get("phase") for e in events if e.source == "evaluator"]
    assert "eval_started" in phases
    assert "eval_scored" in phases
    assert all(e.sandbox_id == seeded["sandbox_id"] for e in events)


async def test_evaluate_missing_scores_returns_empty_and_events(
    seeded, session_factory
) -> None:
    fake = FakeRuntime(scores_obj=None)  # agent never writes scores.json
    rows = await _eval(seeded, session_factory, fake)
    assert rows == []
    assert _scores_in_db(session_factory) == []

    events = _events_in_db(session_factory)
    err = [e for e in events if e.type == "error" and e.source == "evaluator"]
    assert len(err) == 1
    assert err[0].payload["reason"] == "scores_missing"
    assert err[0].payload["phase"] == "eval_scores_failed"


async def test_evaluate_invalid_scores_returns_empty(seeded, session_factory) -> None:
    class BadRuntime(FakeRuntime):
        def spawn(self, spec):
            super().spawn(spec)
            (Path(spec.workspace_host_path) / "scores.json").write_text("not json{{{")

    rows = await _eval(seeded, session_factory, BadRuntime(scores_obj=None))
    assert rows == []
    events = _events_in_db(session_factory)
    err = [e for e in events if e.type == "error" and e.source == "evaluator"]
    assert err[0].payload["reason"] == "scores_invalid"


async def test_evaluate_partial_scores_keeps_valid(seeded, session_factory) -> None:
    partial = {
        "scores": [
            {"dimension": "functional_requirements", "score": 8, "rationale": "ok"},
            {"dimension": "code_quality", "score": "bad"},
        ]
    }
    rows = await _eval(seeded, session_factory, FakeRuntime(scores_obj=partial))
    assert [r.dimension for r in rows] == ["functional_requirements"]
    events = _events_in_db(session_factory)
    scored = [e for e in events if e.payload.get("phase") == "eval_scored"]
    assert scored and scored[0].payload["warnings"]


async def test_evaluate_timeout_still_reads_scores(seeded, session_factory) -> None:
    fake = FakeRuntime(status="running")  # never exits on its own
    rows = await _eval(
        seeded, session_factory, fake, timeout_seconds=0.05, poll_interval_seconds=0.01
    )
    eval_sid = eval_sandbox_id(seeded["sandbox_id"])
    assert fake.stopped == [eval_sid]
    assert fake.removed == [eval_sid]
    # scores.json was written at spawn -> still collected despite timeout
    assert len(rows) == 3
    events = _events_in_db(session_factory)
    scored = [e for e in events if e.payload.get("phase") == "eval_scored"]
    assert scored[0].payload["timed_out"] is True


async def test_evaluate_spawn_failure_raises_and_events(
    seeded, session_factory
) -> None:
    class BoomRuntime(FakeRuntime):
        def spawn(self, spec):
            raise RuntimeError("docker exploded")

    with pytest.raises(RuntimeError, match="docker exploded"):
        await _eval(seeded, session_factory, BoomRuntime())
    events = _events_in_db(session_factory)
    err = [e for e in events if e.type == "error" and e.source == "evaluator"]
    assert err[0].payload["phase"] == "eval_spawn_failed"
    assert _scores_in_db(session_factory) == []


async def test_evaluate_missing_workspace_raises(seeded, session_factory, tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        await _eval(
            seeded,
            session_factory,
            FakeRuntime(),
            workspace_dir=tmp_path / "does-not-exist",
        )


async def test_evaluate_requires_params(seeded, session_factory) -> None:
    with pytest.raises(ValueError, match="model"):
        await _eval(seeded, session_factory, FakeRuntime(), model=None, evaluator_model=None)
    with pytest.raises(ValueError, match="gateway_cred"):
        await _eval(seeded, session_factory, FakeRuntime(), gateway_cred=None, cred=None)
    with pytest.raises(ValueError, match="rubric"):
        await _eval(seeded, session_factory, FakeRuntime(), rubric={})
    with pytest.raises(ValueError, match="session_factory"):
        await _eval(seeded, session_factory, FakeRuntime(), session_factory=None)


async def test_evaluate_alias_params(seeded, session_factory) -> None:
    """Design-brief spellings (evaluator_model/cred/artifact_workspace_path)
    resolve to the same contract."""
    fake = FakeRuntime()
    rows = await evaluate_sandbox(
        run_id=seeded["run_id"],
        sandbox_id=seeded["sandbox_id"],
        rubric=RUBRIC,
        artifact_workspace_path=seeded["workspace"],  # alias for workspace_dir
        evaluator_model="alias-model",  # alias for model
        cred="heg.aliascred",  # alias for gateway_cred
        session_factory=session_factory,
        runtime=fake,
        eval_root=seeded["eval_root"],
        timeout_seconds=30,
        poll_interval_seconds=0.01,
    )
    assert len(rows) == 3
    spec = fake.specs[0]
    assert spec.env["MODEL_NAME"] == "alias-model"
    assert spec.env["GATEWAY_CRED"] == "heg.aliascred"
    # rubric_id fell back to DB lookup (latest approved rubric for the run)
    assert all(r.rubric_id == seeded["rubric_id"] for r in rows)


def test_sandbox_gateway_url_normalization() -> None:
    assert sandbox_gateway_url("http://localhost:8081") == "http://gateway:8080"
    assert sandbox_gateway_url("http://127.0.0.1:8081") == "http://gateway:8080"
    assert sandbox_gateway_url("http://host.docker.internal:11434") == "http://gateway:8080"
    assert sandbox_gateway_url("http://gateway:8080") == "http://gateway:8080"
    assert sandbox_gateway_url("http://gateway:8080/") == "http://gateway:8080"
    assert sandbox_gateway_url("") == "http://gateway:8080"
    # a genuinely remote gateway passes through untouched
    assert sandbox_gateway_url("https://gw.example.com:443") == "https://gw.example.com:443"
