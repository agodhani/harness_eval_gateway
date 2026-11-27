"""evaluate_sandbox tests with a mocked SandboxRuntime (F7).

Hermetic: no docker, no LLM. The fake runtime's ``spawn`` writes a canned
scores.json into the staged workspace (simulating the judge agent), and
``status`` reports an immediate exit — the same contract the real runtime
exposes (spawn/status/stop/remove).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base, Event, Rubric, Run, Sandbox, Score
from evaluator import evaluate as evaluate_mod
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


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    """Duck-typed httpx.AsyncClient for the direct-judge fallback — records
    calls, returns canned chat content or raises a canned exception."""

    def __init__(self, text: str | None = None, exc: Exception | None = None) -> None:
        self._text = text if text is not None else json.dumps(SCORES_OBJ)
        self._exc = exc
        self.calls: list[dict] = []

    async def post(self, url: str, json: dict | None = None, headers: dict | None = None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self._exc is not None:
            raise self._exc
        return _FakeResponse({"choices": [{"message": {"content": self._text}}]})

    async def aclose(self) -> None:
        return None


def _failing_client() -> _FakeClient:
    """Direct-judge client whose gateway call always fails — keeps the
    no-scores tests hermetic (no real POST to http://gateway:8080)."""
    import httpx

    return _FakeClient(exc=httpx.ConnectError("refused"))


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
    # judge_max_tokens unset -> no MAX_TOKENS env (miniloop 4096 default)
    assert "MAX_TOKENS" not in spec.env
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

    # PR6: measured signals staged for the judge + persisted to the artifact
    analysis_md = eval_ws / "ANALYSIS.md"
    analysis_json = eval_ws / "analysis.json"
    assert "Cyclomatic complexity" in analysis_md.read_text()
    report = json.loads(analysis_json.read_text())
    static = next(s for s in report["signals"] if s["name"] == "static_analysis")
    assert static["status"] == "ok"
    assert static["metrics"]["files_analyzed"] == 1  # main.py only
    assert static["metrics"]["languages"]["python"]["files"] == 1
    artifact_report = seeded["artifact_dir"] / "analysis.json"
    assert json.loads(artifact_report.read_text())["schema_version"] == 1

    # evaluator events: eval_analysis + eval_started + eval_scored checkpoints
    events = _events_in_db(session_factory)
    phases = [e.payload.get("phase") for e in events if e.source == "evaluator"]
    assert "eval_analysis" in phases
    assert "eval_started" in phases
    assert "eval_scored" in phases
    analysis_ev = next(e for e in events if e.payload.get("phase") == "eval_analysis")
    assert analysis_ev.payload["status"] == "ok"
    assert analysis_ev.payload["summary"]["files_analyzed"] == 1
    assert all(e.sandbox_id == seeded["sandbox_id"] for e in events)


async def test_evaluate_judge_max_tokens_reaches_env(
    seeded, session_factory
) -> None:
    """judge_max_tokens -> MAX_TOKENS in the eval sandbox env — the raised
    output cap keeps thinking judges from truncating scores.json."""
    fake = FakeRuntime()
    await _eval(seeded, session_factory, fake, judge_max_tokens=8192)
    assert fake.specs[0].env["MAX_TOKENS"] == "8192"


async def test_evaluate_missing_scores_returns_empty_and_events(
    seeded, session_factory
) -> None:
    fake = FakeRuntime(scores_obj=None)  # agent never writes scores.json
    rows = await _eval(seeded, session_factory, fake, direct_client=_failing_client())
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

    rows = await _eval(
        seeded, session_factory, BadRuntime(scores_obj=None),
        direct_client=_failing_client(),
    )
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
    # no `overall` in the doc -> synthesized from the valid entries
    assert [r.dimension for r in rows] == ["functional_requirements", "overall"]
    assert rows[-1].score == 8.0
    events = _events_in_db(session_factory)
    scored = [e for e in events if e.payload.get("phase") == "eval_scored"]
    assert scored and scored[0].payload["warnings"]
    assert (
        "overall missing — computed from dimension scores "
        "(scale-normalized to 0-10)"
    ) in scored[0].payload["warnings"]
    assert scored[0].payload["overall"] == 8.0


async def test_evaluate_genuine_overall_no_synthesis_warning(
    seeded, session_factory
) -> None:
    """A real `overall` lands verbatim — the eval_scored event carries no
    synthesized-mean warning."""
    fake = FakeRuntime()
    rows = await _eval(seeded, session_factory, fake)
    assert {r.dimension for r in rows} == {
        "functional_requirements",
        "code_quality",
        "overall",
    }
    scored = [
        e
        for e in _events_in_db(session_factory)
        if e.payload.get("phase") == "eval_scored"
    ]
    assert scored[0].payload["overall"] == 7.0
    assert not any(
        "computed from dimension scores" in w
        for w in scored[0].payload["warnings"]
    )


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
        await _eval(seeded, session_factory, FakeRuntime(), model=None)
    with pytest.raises(ValueError, match="gateway_cred"):
        await _eval(seeded, session_factory, FakeRuntime(), gateway_cred=None)
    with pytest.raises(ValueError, match="rubric"):
        await _eval(seeded, session_factory, FakeRuntime(), rubric={})
    with pytest.raises(ValueError, match="session_factory"):
        await _eval(seeded, session_factory, FakeRuntime(), session_factory=None)


async def test_evaluate_stamps_rubric_version(seeded, session_factory) -> None:
    """PR5: Score rows carry rubric provenance — resolved from rubric_id
    when no explicit version is passed."""
    rows = await _eval(seeded, session_factory, FakeRuntime())
    assert rows
    assert all(r.rubric_version == 1 for r in rows)  # seeded rubric is v1
    assert all(r.rubric_version == 1 for r in _scores_in_db(session_factory))

    events = _events_in_db(session_factory)
    started = [e for e in events if e.payload.get("phase") == "eval_started"]
    scored = [e for e in events if e.payload.get("phase") == "eval_scored"]
    assert started[0].payload["rubric_id"] == str(seeded["rubric_id"])
    assert started[0].payload["rubric_version"] == 1
    assert scored[0].payload["rubric_version"] == 1


async def test_evaluate_rubric_version_kwarg_wins(seeded, session_factory) -> None:
    """An explicit rubric_version (orchestrator-resolved) stamps rows as-is."""
    rows = await _eval(seeded, session_factory, FakeRuntime(), rubric_version=7)
    assert rows
    assert all(r.rubric_version == 7 for r in rows)


async def test_evaluate_rescore_provenance_distinct(seeded, session_factory) -> None:
    """Two passes over the same artifact under different versions keep
    separate Score rows — the re-score use case."""
    s = session_factory()
    try:
        s.add(
            Rubric(
                run_id=seeded["run_id"], version=2, status="approved",
                parent_id=seeded["rubric_id"], content=RUBRIC,
            )
        )
        s.commit()
        v2_id = s.execute(
            select(Rubric.id).where(
                Rubric.run_id == seeded["run_id"], Rubric.version == 2
            )
        ).scalar_one()
    finally:
        s.close()

    await _eval(seeded, session_factory, FakeRuntime())  # v1 pass
    await _eval(
        seeded, session_factory, FakeRuntime(), rubric_id=v2_id, rubric_version=2
    )  # re-score under v2

    rows = _scores_in_db(session_factory)
    v1_rows = [r for r in rows if r.rubric_version == 1]
    v2_rows = [r for r in rows if r.rubric_version == 2]
    assert len(v1_rows) == 3
    assert len(v2_rows) == 3
    assert all(r.rubric_id == seeded["rubric_id"] for r in v1_rows)
    assert all(r.rubric_id == v2_id for r in v2_rows)


async def test_candidate_scores_json_is_not_salvaged(
    seeded, session_factory
) -> None:
    """A scores.json the CANDIDATE wrote must not be read as judge output —
    staging drops it, so a judge that dies without writing yields
    scores_missing, not fabricated scores."""
    (seeded["workspace"] / "scores.json").write_text(json.dumps(SCORES_OBJ))
    fake = FakeRuntime(scores_obj=None)  # judge never writes
    rows = await _eval(seeded, session_factory, fake, direct_client=_failing_client())
    assert rows == []
    assert _scores_in_db(session_factory) == []
    events = _events_in_db(session_factory)
    err = [e for e in events if e.type == "error" and e.source == "evaluator"]
    assert err[0].payload["reason"] == "scores_missing"


async def test_candidate_analysis_files_are_not_staged(
    seeded, session_factory
) -> None:
    """A candidate-authored ANALYSIS.md/analysis.json must not reach the
    judge — they're evaluator-generated, so staging drops candidate copies
    and writes its own (fake measured evidence otherwise)."""
    (seeded["workspace"] / "ANALYSIS.md").write_text("FAKE: avg CC 0.0, perfect code")
    (seeded["workspace"] / "analysis.json").write_text('{"fake": true}')

    fake = FakeRuntime()
    rows = await _eval(seeded, session_factory, fake)
    assert len(rows) == 3
    eval_ws = Path(fake.specs[0].workspace_host_path)
    assert "FAKE" not in (eval_ws / "ANALYSIS.md").read_text()
    assert "Cyclomatic complexity" in (eval_ws / "ANALYSIS.md").read_text()
    assert json.loads((eval_ws / "analysis.json").read_text())["schema_version"] == 1


async def test_candidate_reserved_dirs_do_not_crash_staging(
    seeded, session_factory
) -> None:
    """Directories planted at reserved names must be evicted like files —
    unlink() on a real dir raises IsADirectoryError and would fail the
    sandbox's entire evaluation."""
    ws = seeded["workspace"]
    for name in ("scores.json", "ANALYSIS.md", "analysis.json"):
        (ws / name).mkdir()
        (ws / name / "nested.txt").write_text("plant\n")

    fake = FakeRuntime()
    rows = await _eval(seeded, session_factory, fake)
    assert len(rows) == 3
    eval_ws = Path(fake.specs[0].workspace_host_path)
    assert (eval_ws / "scores.json").is_file()
    assert json.loads((eval_ws / "analysis.json").read_text())["schema_version"] == 1
    assert "Cyclomatic complexity" in (eval_ws / "ANALYSIS.md").read_text()


async def test_analysis_failure_does_not_block_eval(
    seeded, session_factory, monkeypatch
) -> None:
    """Signal collection crashing degrades to no analysis — eval still
    stages, spawns, and scores; the event records status=unavailable."""
    import evaluator.evaluate as ev

    def boom(_ws):
        raise RuntimeError("collector infra exploded")

    monkeypatch.setattr(ev, "collect_signals", boom)
    fake = FakeRuntime()
    rows = await _eval(seeded, session_factory, fake)
    assert len(rows) == 3
    eval_ws = Path(fake.specs[0].workspace_host_path)
    assert not (eval_ws / "ANALYSIS.md").exists()
    assert not (eval_ws / "analysis.json").exists()
    events = _events_in_db(session_factory)
    analysis_ev = [
        e for e in events if e.payload.get("phase") == "eval_analysis"
    ]
    assert analysis_ev[0].payload["status"] == "unavailable"


async def test_generated_tests_signal_flows_to_judge(
    seeded, session_factory, monkeypatch
) -> None:
    """PR8 wiring: the generated_tests signal merges into analysis.json +
    ANALYSIS.md, the suite is staged into the eval workspace, and a
    test_execution event carries the pass rate to the timeline."""
    import evaluator.evaluate as ev
    from scoring.models import SignalReport

    async def fake_gen(**kwargs):
        # mimic the real helper: write the suite into <artifact>/generated_tests/
        gen_dir = Path(kwargs["gen_dir"])
        gen_dir.mkdir(parents=True, exist_ok=True)
        (gen_dir / "test_calc.py").write_text("def test_x():\n    assert True\n")
        (gen_dir / "_manifest.json").write_text("{}")
        return SignalReport(
            name="generated_tests",
            status="ok",
            metrics={
                "model": "judge-model",
                "files_written": 1,
                "tests_detected": True,
                "tests_collected": 4,
                "tests_passed": 3,
                "tests_failed": 1,
                "tests_errors": 0,
                "tests_skipped": 0,
                "pass_rate": 0.75,
                "image": "heg-exec:latest",
                "timed_out": False,
            },
        )

    monkeypatch.setattr(ev, "generate_tests_signal", fake_gen)
    fake = FakeRuntime()
    rows = await _eval(seeded, session_factory, fake)
    assert len(rows) == 3

    eval_ws = Path(fake.specs[0].workspace_host_path)
    # analysis staged for the judge incl. the new signal + dedicated section
    report = json.loads((eval_ws / "analysis.json").read_text())
    names = [s["name"] for s in report["signals"]]
    assert "generated_tests" in names
    gen = next(s for s in report["signals"] if s["name"] == "generated_tests")
    assert gen["metrics"]["pass_rate"] == 0.75
    assert "Generated test suite" in (eval_ws / "ANALYSIS.md").read_text()
    # suite files staged (evaluator-authored copy)
    assert (eval_ws / "generated_tests" / "test_calc.py").is_file()
    # artifact persists the real report too
    persisted = json.loads((seeded["artifact_dir"] / "analysis.json").read_text())
    assert any(s["name"] == "generated_tests" for s in persisted["signals"])

    events = _events_in_db(session_factory)
    # eval_analysis checkpoint carries the generated_tests summary
    analysis_ev = next(e for e in events if e.payload.get("phase") == "eval_analysis")
    assert analysis_ev.payload["generated_tests"]["tests_passed"] == 3
    # test_execution event with the eval_testgen phase + pass rate
    tex = [
        e for e in events
        if e.type == "test_execution" and e.payload.get("phase") == "eval_testgen"
    ]
    assert len(tex) == 1
    assert tex[0].payload["tests_passed"] == 3
    assert tex[0].payload["pass_rate"] == 0.75


async def test_candidate_generated_tests_dir_is_not_staged(
    seeded, session_factory, monkeypatch
) -> None:
    """A candidate-authored generated_tests/ dir must never reach the judge
    as 'LLM-generated' evidence — the name is reserved; when this attempt
    wrote no suite, the staged workspace has no generated_tests/ at all."""
    import evaluator.evaluate as ev
    from scoring.models import SignalReport

    ws = seeded["workspace"]
    fake_dir = ws / "generated_tests"
    fake_dir.mkdir()
    (fake_dir / "test_fake.py").write_text("def test_fake():\n    assert False\n")

    async def fake_gen(**kwargs):
        return SignalReport(
            name="generated_tests",
            status="skipped",
            warnings=["disabled"],
        )

    monkeypatch.setattr(ev, "generate_tests_signal", fake_gen)
    fake = FakeRuntime()
    rows = await _eval(seeded, session_factory, fake)
    assert len(rows) == 3
    eval_ws = Path(fake.specs[0].workspace_host_path)
    assert not (eval_ws / "generated_tests").exists()


async def test_testgen_failure_does_not_block_eval(
    seeded, session_factory, monkeypatch
) -> None:
    """A raising generator degrades to no signal — eval proceeds normally."""
    import evaluator.evaluate as ev

    async def boom(**kwargs):
        raise RuntimeError("generator exploded")

    monkeypatch.setattr(ev, "generate_tests_signal", boom)
    fake = FakeRuntime()
    rows = await _eval(seeded, session_factory, fake)
    assert len(rows) == 3
    eval_ws = Path(fake.specs[0].workspace_host_path)
    report = json.loads((eval_ws / "analysis.json").read_text())
    assert "generated_tests" not in [s["name"] for s in report["signals"]]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX only")
async def test_candidate_special_files_are_not_staged(
    seeded, session_factory
) -> None:
    """FIFOs in the candidate workspace must not reach the eval workspace —
    copytree's copy2 would block forever opening one."""
    ws = seeded["workspace"]
    os.mkfifo(ws / "pipe")
    sub = ws / "sub"
    sub.mkdir()
    os.mkfifo(sub / "nested-pipe")

    fake = FakeRuntime()
    rows = await _eval(seeded, session_factory, fake)
    assert len(rows) == 3
    eval_ws = Path(fake.specs[0].workspace_host_path)
    assert not os.path.lexists(eval_ws / "pipe")
    assert (eval_ws / "sub").is_dir()
    assert not os.path.lexists(eval_ws / "sub" / "nested-pipe")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX only")
async def test_judge_fifo_scores_is_rejected(seeded, session_factory) -> None:
    """A non-regular scores.json left by the judge is rejected without being
    opened — open() on a FIFO would block the api process forever."""

    class FifoRuntime(FakeRuntime):
        def spawn(self, spec):
            super().spawn(spec)
            os.mkfifo(Path(spec.workspace_host_path) / "scores.json")

    rows = await _eval(
        seeded, session_factory, FifoRuntime(scores_obj=None),
        direct_client=_failing_client(),
    )
    assert rows == []
    assert _scores_in_db(session_factory) == []
    events = _events_in_db(session_factory)
    err = [e for e in events if e.type == "error" and e.source == "evaluator"]
    assert err[0].payload["reason"] == "scores_invalid"


# ---------------------------------------------------------------------------
# direct-judge fallback (evaluator/judge_direct.py wiring)
# ---------------------------------------------------------------------------


async def test_direct_fallback_scores_when_agent_fails(
    seeded, session_factory
) -> None:
    """Agent writes nothing → the control-plane direct call lands scores →
    rows inserted through the normal path + eval_scored mode + scores.json
    rewritten for artifact/debug parity."""
    client = _FakeClient()  # returns SCORES_OBJ as assistant content
    fake = FakeRuntime(scores_obj=None)
    rows = await _eval(seeded, session_factory, fake, direct_client=client)
    assert len(rows) == 3  # 2 dims + overall — same as the agentic path
    assert _scores_in_db(session_factory)

    events = _events_in_db(session_factory)
    scored = [e for e in events if e.payload.get("phase") == "eval_scored"]
    assert len(scored) == 1
    assert scored[0].payload["mode"] == "direct_fallback"
    assert scored[0].payload["overall"] == 7.0
    assert not [
        e for e in events if e.payload.get("phase") == "eval_scores_failed"
    ]

    # parsed doc written back over eval_ws/scores.json
    eval_ws = Path(fake.specs[0].workspace_host_path)
    assert json.loads((eval_ws / "scores.json").read_text())["overall"] == 7.0

    # the call went through the gateway with the eval cred + json_object
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["headers"]["Authorization"] == "Bearer heg.testcred"
    assert call["json"]["response_format"] == {"type": "json_object"}
    assert call["json"]["model"] == "judge-model"
    # current attempt's parse failure is echoed in the direct prompt
    user = call["json"]["messages"][1]["content"]
    assert "scores_missing" in user


async def test_direct_fallback_failure_emits_fallback_detail(
    seeded, session_factory
) -> None:
    """Both judge paths fail → eval_scores_failed carries the agentic detail
    AND the fallback's failure so the timeline shows both."""
    import httpx

    client = _FakeClient(exc=httpx.ConnectError("gateway unreachable"))
    rows = await _eval(
        seeded, session_factory, FakeRuntime(scores_obj=None), direct_client=client
    )
    assert rows == []
    events = _events_in_db(session_factory)
    err = [
        e
        for e in events
        if e.type == "error" and e.payload.get("phase") == "eval_scores_failed"
    ]
    assert len(err) == 1
    assert err[0].payload["reason"] == "scores_missing"
    assert "gateway unreachable" in err[0].payload["fallback_detail"]


async def test_direct_fallback_disabled_by_env(
    seeded, session_factory, monkeypatch
) -> None:
    """EVAL_DIRECT_FALLBACK=0 — no gateway call at all; the disable reason
    still lands in fallback_detail."""
    monkeypatch.setenv("EVAL_DIRECT_FALLBACK", "0")
    client = _FakeClient()
    rows = await _eval(
        seeded, session_factory, FakeRuntime(scores_obj=None), direct_client=client
    )
    assert rows == []
    assert client.calls == []
    events = _events_in_db(session_factory)
    err = [
        e for e in events if e.payload.get("phase") == "eval_scores_failed"
    ]
    assert "disabled" in err[0].payload["fallback_detail"]


async def test_direct_fallback_bounded_by_attempt_deadline(
    seeded, session_factory
) -> None:
    """A fallback that would outlive the attempt's deadline is cancelled
    instead — expiry lands in fallback_detail so the orchestrator's retry
    loop stays in charge (an outer wait_for cancellation would break out
    of it as a deterministic failure)."""

    class SlowClient(_FakeClient):
        async def post(self, url, json=None, headers=None):
            await asyncio.sleep(30)
            return _FakeResponse({})

    rows = await _eval(
        seeded,
        session_factory,
        FakeRuntime(scores_obj=None),
        direct_client=SlowClient(),
        timeout_seconds=6.0,
        deadline_margin_seconds=0.5,
    )
    assert rows == []
    err = [
        e
        for e in _events_in_db(session_factory)
        if e.payload.get("phase") == "eval_scores_failed"
    ]
    assert len(err) == 1
    assert "deadline" in err[0].payload["fallback_detail"]


async def test_direct_fallback_skipped_when_budget_exhausted(
    seeded, session_factory
) -> None:
    """No attempt budget left → no gateway call at all."""
    client = _FakeClient()
    rows = await _eval(
        seeded,
        session_factory,
        FakeRuntime(scores_obj=None),
        direct_client=client,
        timeout_seconds=0.0,
        deadline_margin_seconds=0.0,
    )
    assert rows == []
    assert client.calls == []
    err = [
        e
        for e in _events_in_db(session_factory)
        if e.payload.get("phase") == "eval_scores_failed"
    ]
    assert "deadline" in err[0].payload["fallback_detail"]


async def test_eval_scored_mode_is_agentic_on_normal_path(
    seeded, session_factory
) -> None:
    fake = FakeRuntime()
    rows = await _eval(seeded, session_factory, fake, direct_client=_FakeClient())
    assert len(rows) == 3
    events = _events_in_db(session_factory)
    scored = [e for e in events if e.payload.get("phase") == "eval_scored"]
    assert scored[0].payload["mode"] == "agentic"


async def test_prior_error_threads_into_eval_prompt(
    seeded, session_factory
) -> None:
    """Error-feedback retry: prior_error lands in EVAL_PROMPT.md so the
    respawned judge sees why its previous output was rejected."""
    fake = FakeRuntime()
    await _eval(
        seeded,
        session_factory,
        fake,
        prior_error="scores_invalid: single-quoted strings",
    )
    prompt = (Path(fake.specs[0].workspace_host_path) / "EVAL_PROMPT.md").read_text()
    assert "Previous attempt failed" in prompt
    assert "single-quoted strings" in prompt
    assert "ONE JSON object" in prompt


async def test_prior_error_reaches_direct_prompt(
    seeded, session_factory
) -> None:
    """The fallback prompt sees BOTH the orchestrator retry feedback and the
    current attempt's parse failure."""
    client = _FakeClient()
    await _eval(
        seeded,
        session_factory,
        FakeRuntime(scores_obj=None),
        prior_error="earlier attempt: wandered off",
        direct_client=client,
    )
    user = client.calls[0]["json"]["messages"][1]["content"]
    assert "wandered off" in user
    assert "scores_missing" in user


def test_sandbox_gateway_url_normalization() -> None:
    assert sandbox_gateway_url("http://localhost:8081") == "http://gateway:8080"
    assert sandbox_gateway_url("http://127.0.0.1:8081") == "http://gateway:8080"
    assert sandbox_gateway_url("http://host.docker.internal:11434") == "http://gateway:8080"
    assert sandbox_gateway_url("http://gateway:8080") == "http://gateway:8080"
    assert sandbox_gateway_url("http://gateway:8080/") == "http://gateway:8080"
    assert sandbox_gateway_url("") == "http://gateway:8080"
    # a genuinely remote gateway passes through untouched
    assert sandbox_gateway_url("https://gw.example.com:443") == "https://gw.example.com:443"


# ---------------------------------------------------------------------------
# PR25 — output-only (prompt cell) submissions
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded_prompt(session_factory: sessionmaker[Session], tmp_path: Path):
    """A prompt-cell artifact dir: output.txt + transcript, NO workspace."""
    artifact_dir = tmp_path / "prompt_artifact"
    artifact_dir.mkdir(parents=True)
    output = artifact_dir / "output.txt"
    output.write_text("The extracted total is 42 EUR.\n")
    transcript = artifact_dir / "transcript.log"
    transcript.write_text("=== user ===\nextract the total\n=== assistant ===\n42 EUR\n")

    s = session_factory()
    try:
        run = Run(task_prompt="extract the total", model_name="m", config={})
        s.add(run)
        s.flush()
        sb = Sandbox(
            run_id=run.id,
            harness_config={"adapter": "prompt"},
            status="done",
            terminal_reason="harness_complete",
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
        "output": output,
        "transcript": transcript,
        "eval_root": tmp_path / "eval",
    }


async def test_evaluate_prompt_output_submission(
    seeded_prompt, session_factory
) -> None:
    """prompt_output_path: output.txt is staged as the submission — the
    judge reads it like workspace content, no workspace dir required."""
    fake = FakeRuntime()
    rows = await evaluate_sandbox(
        run_id=seeded_prompt["run_id"],
        sandbox_id=seeded_prompt["sandbox_id"],
        rubric_id=seeded_prompt["rubric_id"],
        rubric=RUBRIC,
        artifact_dir=seeded_prompt["artifact_dir"],
        # no workspace_dir — a prompt cell has none; prompt_output_path
        # stands in for it.
        prompt_output_path=seeded_prompt["output"],
        transcript_path=seeded_prompt["transcript"],
        terminal_reason="harness_complete",
        model="judge-model",
        gateway_cred="heg.testcred",
        session_factory=session_factory,
        runtime=fake,
        eval_root=seeded_prompt["eval_root"],
        timeout_seconds=30,
        poll_interval_seconds=0.01,
        max_iters=5,
    )
    assert len(rows) == 3  # same score persistence path as workspaces

    eval_ws = Path(fake.specs[0].workspace_host_path)
    # the staged submission IS output.txt — readable by the judge
    assert (eval_ws / "output.txt").read_text() == "The extracted total is 42 EUR.\n"
    # transcript still lands as reference material
    assert (eval_ws / "EVAL_TRANSCRIPT.log").is_file()
    # EVAL_PROMPT frames output.txt as the submission, not a code repo
    prompt = (eval_ws / "EVAL_PROMPT.md").read_text()
    assert "output.txt" in prompt
    assert "prompt-only" in prompt or "prompt" in prompt.lower()


@pytest.mark.parametrize("content", [None, "", " \n\t"])
async def test_evaluate_prompt_output_missing_or_blank_is_not_scored(
    seeded_prompt, session_factory, content
) -> None:
    fake = FakeRuntime()
    output = seeded_prompt["output"]
    if content is None:
        output.unlink()
    else:
        output.write_text(content)
    with pytest.raises(FileNotFoundError, match="prompt output"):
        await _eval(
            {**seeded_prompt, "workspace": None},
            session_factory,
            fake,
            prompt_output_path=output,
        )
    assert not fake.specs
    assert not _scores_in_db(session_factory)


async def test_evaluate_workspace_with_only_empty_files_and_scaffolding_is_not_scored(
    seeded, session_factory
) -> None:
    workspace = seeded["workspace"]
    (workspace / "main.py").unlink()
    (workspace / "hello.txt").write_text("")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("git metadata")
    (workspace / ".gitignore").write_text(".aider*\n")
    (workspace / ".aider.chat.history.md").write_text("Aider scaffolding")
    (workspace / ".aider.tags.cache.v4").mkdir()
    (workspace / ".aider.tags.cache.v4" / "cache.db").write_text("cache")
    (workspace / "scores.json").write_text('{"overall": 10}')
    (workspace / "nested").mkdir()
    (workspace / "link.txt").symlink_to(seeded["transcript"])
    fake = FakeRuntime()
    with pytest.raises(FileNotFoundError, match="workspace has no non-empty submission"):
        await _eval(seeded, session_factory, fake)
    assert not fake.specs
    assert not _scores_in_db(session_factory)


@pytest.mark.parametrize("path", [".gitignore", ".github/workflows/ci.yml", ".vscode/settings.json"])
async def test_evaluate_workspace_with_only_hidden_deliverable(
    seeded, session_factory, path
) -> None:
    workspace = seeded["workspace"]
    (workspace / "main.py").unlink()
    (workspace / "hello.txt").unlink()
    deliverable = workspace / path
    deliverable.parent.mkdir(parents=True, exist_ok=True)
    deliverable.write_text("submission\n")

    fake = FakeRuntime()
    rows = await _eval(seeded, session_factory, fake)

    assert len(rows) == 3
    assert (Path(fake.specs[0].workspace_host_path) / path).read_text() == "submission\n"


@pytest.mark.parametrize("change", [None, "add", "modify", "mode", "aider", "aider-and-rule"])
async def test_seed_only_workspace_never_reaches_judges_or_score_parser(
    seeded, session_factory, tmp_path, monkeypatch, change
) -> None:
    workspace = seeded["workspace"]
    (workspace / "main.py").unlink()
    (workspace / "hello.txt").unlink()
    starter = "Starter README\n"
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "README.md").write_text(starter)
    (workspace / "README.md").write_text(starter)
    if change == "mode":
        for directory in (seed, workspace):
            (directory / "run.sh").write_text("#!/bin/sh\n")
            (directory / "run.sh").chmod(0o644)
        (workspace / "run.sh").chmod(0o755)
    if change in ("aider", "aider-and-rule"):
        (seed / ".gitignore").write_text("build/")
        suffix = "\ndist/" if change == "aider-and-rule" else ""
        (workspace / ".gitignore").write_text(f"build/\n\n.aider*{suffix}\n")
    fake = FakeRuntime()

    if change in (None, "aider"):
        def unexpected(*args, **kwargs):
            pytest.fail("empty submission reached judging or score parsing")

        monkeypatch.setattr(evaluate_mod, "judge_direct", unexpected)
        monkeypatch.setattr(evaluate_mod, "load_scores_file", unexpected)
        with pytest.raises(FileNotFoundError, match="no non-empty submission files"):
            await _eval(seeded, session_factory, fake, seed_dir=seed)
        assert not fake.specs
        assert not _scores_in_db(session_factory)
        return
    if change == "modify":
        (workspace / "README.md").write_text(starter.replace("Starter", "Changed"))
    elif change == "add":
        (workspace / ".deliverable").write_text("new work")
    assert len(await _eval(seeded, session_factory, fake, seed_dir=seed)) == 3


async def test_evaluate_prompt_output_analysis_persists(
    seeded_prompt, session_factory
) -> None:
    """Collectors degrade gracefully on a .txt-only submission and the
    analysis artifact still lands for the results API."""
    fake = FakeRuntime()
    await evaluate_sandbox(
        run_id=seeded_prompt["run_id"],
        sandbox_id=seeded_prompt["sandbox_id"],
        rubric_id=seeded_prompt["rubric_id"],
        rubric=RUBRIC,
        artifact_dir=seeded_prompt["artifact_dir"],
        prompt_output_path=seeded_prompt["output"],
        transcript_path=seeded_prompt["transcript"],
        terminal_reason="harness_complete",
        model="judge-model",
        gateway_cred="heg.testcred",
        session_factory=session_factory,
        runtime=fake,
        eval_root=seeded_prompt["eval_root"],
        timeout_seconds=30,
        poll_interval_seconds=0.01,
        max_iters=5,
    )
    analysis = seeded_prompt["artifact_dir"] / "analysis.json"
    assert analysis.is_file()
    report = json.loads(analysis.read_text())
    static = next(s for s in report["signals"] if s["name"] == "static_analysis")
    # output.txt isn't a source language — 0 files analyzed, not an error
    assert static["metrics"]["files_analyzed"] == 0


# ---------------------------------------------------------------------------
# PR29 — judge attribution
# ---------------------------------------------------------------------------


async def test_evaluate_stamps_judge_model_on_rows_and_events(
    seeded, session_factory
) -> None:
    """PR29 — every Score row + the eval_scored checkpoint names the
    judging model; a panel keeps each judge's verdict attributable."""
    rows = await _eval(seeded, session_factory, FakeRuntime(), model="judge-b")
    assert rows
    assert all(r.judge_model == "judge-b" for r in rows)
    assert all(r.source == "llm" for r in rows)

    db_rows = _scores_in_db(session_factory)
    assert {r.judge_model for r in db_rows} == {"judge-b"}
    assert {r.source for r in db_rows} == {"llm"}

    scored = next(
        e for e in _events_in_db(session_factory)
        if e.payload.get("phase") == "eval_scored"
    )
    assert scored.payload["judge_model"] == "judge-b"


async def test_evaluate_failure_event_names_judge(
    seeded, session_factory
) -> None:
    """eval_scores_failed carries judge_model — the timeline attributes a
    panel member's failure to that judge, not 'the evaluator'."""
    fake = FakeRuntime(scores_obj=None)
    rows = await _eval(
        seeded, session_factory, fake,
        model="judge-b", direct_client=_failing_client(),
    )
    assert rows == []
    err = next(
        e for e in _events_in_db(session_factory)
        if e.payload.get("phase") == "eval_scores_failed"
    )
    assert err.payload["judge_model"] == "judge-b"
