"""Re-score (PR5): rescore_run + create_rubric_version + runner task wiring.

Hermetic — same fixtures as test_lifecycle.py (sqlite + FakeRuntime); the
evaluator is a fake that writes Score rows like the F7 contract. Artifact
dirs are materialized on disk so ``_sandboxes_with_artifacts`` and the
evaluator kwargs see real paths.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select

from db.models import Event, Rubric, Run, Sandbox, Score
from orchestrator import runner
from orchestrator.lifecycle import rescore_run
from orchestrator.rubric import RubricContent, create_rubric_version

from .conftest import FakeRuntime, make_run, make_sandbox, run_config

DIMS = [
    "functional_requirements",
    "acceptance_criteria",
    "code_quality",
    "cyclomatic_complexity",
]

RUBRIC_CONTENT = {"summary": "s", "dimensions": [], "criteria": []}


def _rubric(
    session_factory, run_id, *, version: int, status: str = "approved"
) -> Rubric:
    with session_factory() as s:
        r = Rubric(
            run_id=run_id,
            version=version,
            status=status,
            content=dict(RUBRIC_CONTENT),
        )
        s.add(r)
        s.commit()
        return r


def _artifact_dir(artifact_root: Path, run_id: uuid.UUID, sb_id: uuid.UUID) -> Path:
    d = artifact_root / str(run_id) / str(sb_id)
    (d / "workspace").mkdir(parents=True)
    (d / "workspace" / "hello.txt").write_text("hi\n")
    (d / "transcript.log").write_text("MINILOOP_RESULT {}\n")
    return d


def _artifact_sandbox(
    session_factory, artifact_root: Path, run_id: uuid.UUID, *, status: str = "done"
) -> Sandbox:
    """Sandbox row whose artifact_path points at a real on-disk snapshot."""
    # id needed for the path — create row first, then backfill artifact_path.
    sb = make_sandbox(session_factory, run_id, status=status, terminal_reason="agent_done")
    path = _artifact_dir(artifact_root, run_id, sb.id)
    with session_factory() as s:
        row = s.get(Sandbox, sb.id)
        row.artifact_path = str(path)
        s.commit()
    return row


def _score_writing_evaluator(calls: list, dims=DIMS, fail_for: set | None = None):
    """Fake F7 evaluator: records kwargs, writes Score rows like the contract."""

    async def fake_eval(**kwargs):
        calls.append(kwargs)
        if fail_for and kwargs["sandbox_id"] in fail_for:
            raise RuntimeError("eval boom")
        with kwargs["session_factory"]() as s:
            for d in dims:
                s.add(
                    Score(
                        sandbox_id=kwargs["sandbox_id"],
                        rubric_id=kwargs["rubric_id"],
                        rubric_version=kwargs.get("rubric_version"),
                        dimension=d,
                        score=8.0,
                        rationale="looks fine",
                    )
                )
            s.commit()
        return []

    return fake_eval


def _events(session_factory, run_id) -> list[Event]:
    with session_factory() as s:
        return list(s.execute(select(Event).where(Event.run_id == run_id)).scalars())


def _run(session_factory, run_id) -> Run | None:
    with session_factory() as s:
        return s.get(Run, run_id)


def _scores(session_factory, **where) -> list[Score]:
    stmt = select(Score)
    for col, val in where.items():
        stmt = stmt.where(getattr(Score, col) == val)
    with session_factory() as s:
        return list(s.execute(stmt).scalars())


# ---------------------------------------------------------------------------
# rescore_run
# ---------------------------------------------------------------------------


async def test_rescore_new_version_done_run(session_factory, settings, artifact_root) -> None:
    """Re-score against an explicit rubric version → new Score rows carry
    that version's provenance; old v1 rows untouched; run back to done."""
    run = make_run(session_factory, status="done", config=run_config())
    v1 = _rubric(session_factory, run.id, version=1, status="superseded")
    v2 = _rubric(session_factory, run.id, version=2)
    sb = _artifact_sandbox(session_factory, artifact_root, run.id)

    calls: list = []
    await rescore_run(
        run.id,
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
        rubric_id=v2.id,
    )

    assert len(calls) == 1
    kw = calls[0]
    assert kw["rubric_id"] == v2.id
    assert kw["rubric_version"] == 2
    assert kw["model"] == "eval-model"  # run.config evaluator_model
    assert kw["sandbox_id"] == sb.id

    r = _run(session_factory, run.id)
    assert r.status == "done"
    assert r.finished_at is not None

    new_scores = _scores(session_factory, rubric_id=v2.id)
    assert len(new_scores) == len(DIMS)
    assert all(sc.rubric_version == 2 for sc in new_scores)
    assert _scores(session_factory, rubric_id=v1.id) == []

    stages = [e.payload.get("stage") for e in _events(session_factory, run.id)]
    assert "rescore_started" in stages
    assert "rescore_finished" in stages
    finished = next(e for e in _events(session_factory, run.id)
                    if e.payload.get("stage") == "rescore_finished")
    assert finished.payload["rubric_version"] == 2
    assert finished.payload["scores_for_rubric"] == len(DIMS)


async def test_rescore_defaults_to_latest_approved(
    session_factory, settings, artifact_root
) -> None:
    """No rubric_id → latest approved version (superseded rows skipped)."""
    run = make_run(session_factory, status="done", config=run_config())
    _rubric(session_factory, run.id, version=1, status="superseded")
    v2 = _rubric(session_factory, run.id, version=2)
    _artifact_sandbox(session_factory, artifact_root, run.id)

    calls: list = []
    await rescore_run(
        run.id,
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )

    assert calls[0]["rubric_id"] == v2.id
    assert calls[0]["rubric_version"] == 2
    assert _run(session_factory, run.id).status == "done"


async def test_rescore_failed_run_recovers_to_done(
    session_factory, settings, artifact_root
) -> None:
    """A run that failed evaluation gets a second chance: rescore → done."""
    run = make_run(session_factory, status="failed", config=run_config())
    _rubric(session_factory, run.id, version=1)
    _artifact_sandbox(session_factory, artifact_root, run.id)

    calls: list = []
    await rescore_run(
        run.id,
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )

    r = _run(session_factory, run.id)
    assert r.status == "done"
    assert r.finished_at is not None
    assert len(calls) == 1


async def test_rescore_no_new_scores_restores_done(
    session_factory, settings, artifact_root
) -> None:
    """Judge produces nothing on rescore, but old scores still exist → the
    run returns to done, not failed."""
    run = make_run(session_factory, status="done", config=run_config())
    rubric = _rubric(session_factory, run.id, version=1)
    sb = _artifact_sandbox(session_factory, artifact_root, run.id)
    # pre-existing scores from the original eval pass
    with session_factory() as s:
        s.add(
            Score(
                sandbox_id=sb.id,
                rubric_id=rubric.id,
                rubric_version=1,
                dimension="functional_requirements",
                score=7.0,
            )
        )
        s.commit()

    async def empty_eval(**kwargs):
        return []

    await rescore_run(
        run.id,
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        settings=settings,
        evaluator_fn=empty_eval,
    )

    assert _run(session_factory, run.id).status == "done"


async def test_rescore_zero_scores_total_stays_failed(
    session_factory, settings, artifact_root
) -> None:
    """Failed run + judge still produces nothing → stays failed."""
    run = make_run(session_factory, status="failed", config=run_config())
    _rubric(session_factory, run.id, version=1)
    _artifact_sandbox(session_factory, artifact_root, run.id)
    settings.EVAL_SCORE_RETRIES = 0  # no retry noise

    async def empty_eval(**kwargs):
        return []

    await rescore_run(
        run.id,
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        settings=settings,
        evaluator_fn=empty_eval,
    )

    assert _run(session_factory, run.id).status == "failed"
    finished = [
        e for e in _events(session_factory, run.id)
        if e.payload.get("stage") == "rescore_finished"
    ]
    assert finished[0].payload["to_status"] == "failed"


async def test_rescore_sandbox_subset(session_factory, settings, artifact_root) -> None:
    """sandbox_ids limits the re-score to matching artifact sandboxes."""
    run = make_run(session_factory, status="done", config=run_config())
    _rubric(session_factory, run.id, version=1)
    sb1 = _artifact_sandbox(session_factory, artifact_root, run.id)
    sb2 = _artifact_sandbox(session_factory, artifact_root, run.id)

    calls: list = []
    await rescore_run(
        run.id,
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
        sandbox_ids=[sb1.id],
    )

    assert [c["sandbox_id"] for c in calls] == [sb1.id]
    assert _scores(session_factory, sandbox_id=sb2.id) == []
    started = [
        e for e in _events(session_factory, run.id)
        if e.payload.get("stage") == "rescore_started"
    ]
    assert started[0].payload["sandbox_ids"] == [str(sb1.id)]


async def test_rescore_model_override(session_factory, settings, artifact_root) -> None:
    run = make_run(session_factory, status="done", config=run_config())
    _rubric(session_factory, run.id, version=1)
    _artifact_sandbox(session_factory, artifact_root, run.id)

    calls: list = []
    await rescore_run(
        run.id,
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
        model="other-judge",
    )

    assert calls[0]["model"] == "other-judge"


async def test_rescore_no_approved_rubric(session_factory, settings, artifact_root) -> None:
    """Only a draft rubric exists → error event + terminal status restored."""
    run = make_run(session_factory, status="done", config=run_config())
    _rubric(session_factory, run.id, version=1, status="draft")
    _artifact_sandbox(session_factory, artifact_root, run.id)

    calls: list = []
    await rescore_run(
        run.id,
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )

    assert calls == []
    errs = [
        e for e in _events(session_factory, run.id)
        if e.type == "error" and e.payload.get("stage") == "rescore"
    ]
    assert errs and "no approved rubric" in errs[0].payload["message"]


async def test_rescore_unknown_rubric_id(session_factory, settings, artifact_root) -> None:
    run = make_run(session_factory, status="done", config=run_config())
    _rubric(session_factory, run.id, version=1)
    _artifact_sandbox(session_factory, artifact_root, run.id)

    calls: list = []
    await rescore_run(
        run.id,
        session_factory=session_factory,
        runtime=FakeRuntime(artifact_root=artifact_root),
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
        rubric_id=uuid.uuid4(),  # does not exist
    )

    assert calls == []
    errs = [
        e for e in _events(session_factory, run.id)
        if e.type == "error" and e.payload.get("stage") == "rescore"
    ]
    assert errs


# ---------------------------------------------------------------------------
# create_rubric_version
# ---------------------------------------------------------------------------


def test_create_rubric_version_supersedes_and_parents(session_factory) -> None:
    run = make_run(session_factory, status="done", config=run_config())
    v1 = _rubric(session_factory, run.id, version=1)

    content = RubricContent.model_validate(
        {
            "summary": "tightened",
            "dimensions": [{"name": d} for d in DIMS],
            "criteria": [],
        }
    )
    with session_factory() as s:
        run_row = s.get(Run, run.id)
        v2 = create_rubric_version(s, run_row, content)
        s.commit()
        v2_id = v2.id

    with session_factory() as s:
        rows = {
            r.version: r
            for r in s.execute(
                select(Rubric).where(Rubric.run_id == run.id)
            ).scalars()
        }
        assert rows[1].status == "superseded"
        assert rows[2].status == "approved"
        assert rows[2].parent_id == v1.id
        assert rows[2].content["summary"] == "tightened"

    stages = [e.payload.get("stage") for e in _events(session_factory, run.id)]
    assert "rubric_version_created" in stages
    ev = next(
        e for e in _events(session_factory, run.id)
        if e.payload.get("stage") == "rubric_version_created"
    )
    assert ev.payload["version"] == 2
    assert ev.payload["parent_version"] == 1
    assert ev.payload["rubric_id"] == str(v2_id)


def test_create_rubric_version_explicit_base(session_factory) -> None:
    """base= pins the parent even when it isn't the latest version."""
    run = make_run(session_factory, status="done", config=run_config())
    v1 = _rubric(session_factory, run.id, version=1, status="superseded")
    _rubric(session_factory, run.id, version=2)

    content = RubricContent.model_validate(
        {"summary": "from v1", "dimensions": [{"name": d} for d in DIMS]}
    )
    with session_factory() as s:
        run_row = s.get(Run, run.id)
        base = s.get(Rubric, v1.id)
        v3 = create_rubric_version(s, run_row, content, base=base)
        s.commit()
        assert v3.version == 3
        assert v3.parent_id == v1.id


# ---------------------------------------------------------------------------
# runner task wiring
# ---------------------------------------------------------------------------


async def test_start_rescore_task_registers_and_dedups(
    session_factory, settings, artifact_root, monkeypatch
) -> None:
    """start_rescore_task → rescore_run via the registry; in_flight while
    running; a second trigger returns the in-flight task."""
    run = make_run(session_factory, status="done", config=run_config())
    _rubric(session_factory, run.id, version=1)
    _artifact_sandbox(session_factory, artifact_root, run.id)

    import asyncio

    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_eval(**kwargs):
        entered.set()
        await release.wait()
        with kwargs["session_factory"]() as s:
            s.add(
                Score(
                    sandbox_id=kwargs["sandbox_id"],
                    rubric_id=kwargs["rubric_id"],
                    dimension="d",
                    score=9.0,
                )
            )
            s.commit()
        return []

    task = runner.start_rescore_task(
        run.id,
        session_factory=session_factory,
        settings=settings,
        runtime=FakeRuntime(artifact_root=artifact_root),
        evaluator_fn=slow_eval,
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert runner.in_flight(run.id, "rescore") is True
    # dedup: second call returns the same in-flight task
    assert runner.start_rescore_task(
        run.id,
        session_factory=session_factory,
        settings=settings,
        runtime=FakeRuntime(artifact_root=artifact_root),
        evaluator_fn=slow_eval,
    ) is task

    release.set()
    await asyncio.wait_for(task, timeout=5)
    assert runner.in_flight(run.id, "rescore") is False
    assert _run(session_factory, run.id).status == "done"


async def test_sweep_rescues_mid_rescore_run_with_scores(
    session_factory, settings, artifact_root
) -> None:
    """api restart mid-RE-SCORE keeps prior scores → done (not failed).

    The rescore_requested checkpoint (emitted in the same transaction as
    the done → evaluating transition) is what marks this as a re-score
    pass rather than a first-pass eval.
    """
    run = make_run(session_factory, status="evaluating", config=run_config())
    rubric = _rubric(session_factory, run.id, version=1)
    sb = _artifact_sandbox(session_factory, artifact_root, run.id)
    with session_factory() as s:
        s.add(
            Score(
                sandbox_id=sb.id,
                rubric_id=rubric.id,
                rubric_version=1,
                dimension="functional_requirements",
                score=7.0,
            )
        )
        s.add(
            Event(
                run_id=run.id,
                source="orchestrator",
                type="checkpoint",
                payload={"stage": "rescore_requested", "rubric_version": 1},
            )
        )
        s.commit()

    await runner.startup_sweep(
        session_factory=session_factory,
        settings=settings,
        runtime=FakeRuntime(artifact_root=artifact_root),
    )

    assert _run(session_factory, run.id).status == "done"


async def test_sweep_fails_first_pass_eval_with_scores(
    session_factory, settings, artifact_root
) -> None:
    """Mid-FIRST-PASS eval with partial scores still fails — the rescue
    only applies to re-score passes (rescore checkpoint present)."""
    run = make_run(session_factory, status="evaluating", config=run_config())
    rubric = _rubric(session_factory, run.id, version=1)
    sb = _artifact_sandbox(session_factory, artifact_root, run.id)
    with session_factory() as s:
        s.add(
            Score(
                sandbox_id=sb.id,
                rubric_id=rubric.id,
                rubric_version=1,
                dimension="functional_requirements",
                score=7.0,
            )
        )
        s.commit()

    await runner.startup_sweep(
        session_factory=session_factory,
        settings=settings,
        runtime=FakeRuntime(artifact_root=artifact_root),
    )

    assert _run(session_factory, run.id).status == "failed"


async def test_sweep_fails_evaluating_run_without_scores(
    session_factory, settings, artifact_root
) -> None:
    """Mid-eval restart with nothing scored yet → failed (unchanged rule)."""
    run = make_run(session_factory, status="evaluating", config=run_config())
    _rubric(session_factory, run.id, version=1)
    _artifact_sandbox(session_factory, artifact_root, run.id)

    await runner.startup_sweep(
        session_factory=session_factory,
        settings=settings,
        runtime=FakeRuntime(artifact_root=artifact_root),
    )

    assert _run(session_factory, run.id).status == "failed"
