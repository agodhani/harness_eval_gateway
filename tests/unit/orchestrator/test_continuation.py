"""PR16 continuations: respawn a finished sandbox on its live workspace.

Hermetic like the rest of this suite: file-backed sqlite + FakeRuntime.
The API-side placement (slot claim, harness_config write) lives in
``api/routers/runs.py`` — these tests start at the post-accept state:
run ``provisioning``, sandbox ``provisioning`` with a
``harness_config.continuations`` entry.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from db.models import Artifact, Event, Rubric, Run, Sandbox, Score
from orchestrator import continuation
from orchestrator.continuation import run_continuation

from .conftest import FakeRuntime, make_run, run_config

DIMS = [
    "functional_requirements",
    "acceptance_criteria",
    "code_quality",
    "cyclomatic_complexity",
]

RESULT_DONE = "MINILOOP_RESULT " + json.dumps(
    {"terminal_state": "agent_done", "iters": 1}
) + "\n"


def _approved_rubric(session_factory, run_id) -> Rubric:
    with session_factory() as s:
        r = Rubric(
            run_id=run_id,
            version=1,
            status="approved",
            content={"summary": "s", "dimensions": [], "criteria": []},
        )
        s.add(r)
        s.commit()
        return r


def _score_writing_evaluator(calls: list):
    """Fake F7 evaluator — records kwargs, writes Score rows."""

    async def fake_eval(**kwargs):
        calls.append(kwargs)
        with kwargs["session_factory"]() as s:
            for d in DIMS:
                s.add(
                    Score(
                        sandbox_id=kwargs["sandbox_id"],
                        rubric_id=kwargs["rubric_id"],
                        dimension=d,
                        score=8.0,
                        rationale="looks fine",
                    )
                )
            s.commit()
        return []

    return fake_eval


def _continuation_sandbox(
    session_factory,
    run_id: uuid.UUID,
    *,
    phase: int = 2,
    prompt: str = "add tests",
    artifact_path: str | None = None,
    status: str = "provisioning",
) -> Sandbox:
    """Sandbox row in post-accept state: provisioning + continuations entry."""
    with session_factory() as s:
        sb = Sandbox(
            run_id=run_id,
            harness_config={
                "adapter": "miniloop",
                "skill_packs": [],
                "system_prompt_extra": None,
                "continuations": [
                    {
                        "phase": phase,
                        "prompt": prompt,
                        "started_at": datetime.now(UTC).isoformat(),
                    }
                ],
            },
            status=status,
            started_at=datetime.now(UTC),
            artifact_path=artifact_path,
        )
        s.add(sb)
        s.commit()
        return sb


def _add_event(session_factory, run_id, sandbox_id, etype, payload, ts=None):
    kwargs = {} if ts is None else {"ts": ts}
    with session_factory() as s:
        s.add(
            Event(
                run_id=run_id,
                sandbox_id=sandbox_id,
                source="gateway" if etype in ("error", "llm_response") else "harness",
                type=etype,
                payload=payload,
                **kwargs,
            )
        )
        s.commit()


def _get(session_factory, model, row_id) -> Any:
    with session_factory() as s:
        return s.get(model, row_id)


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


async def test_continuation_happy_path(
    session_factory, settings, artifact_root
) -> None:
    """Phase 2 respawns the SAME sandbox id on its live workspace, re-snapshots
    under continuations/2, re-scores only that sandbox, run closes done."""
    cfg = run_config()
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)
    phase1_dir = artifact_root / str(run.id) / "sb1"  # prior artifact path
    sb = _continuation_sandbox(
        session_factory, run.id, artifact_path=str(phase1_dir)
    )

    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)
    calls: list = []
    await run_continuation(
        run.id,
        sb.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )

    # same sandbox id respawned — one fresh container spec
    assert len(fake.spawned) == 1
    spec = fake.spawned[0]
    assert spec.sandbox_id == str(sb.id)
    assert spec.run_id == str(run.id)
    env = spec.env
    assert env["GATEWAY_CRED"].startswith("heg.")
    # follow-up prompt wrapped with original task + continuation note
    task = env["TASK_PROMPT"]
    assert "create hello.txt" in task  # original task_prompt
    assert "continuation — phase 2" in task
    assert "add tests" in task
    # same live workspace dir as phase 1 (state_dir/<sid>/workspace)
    assert str(spec.workspace_host_path).endswith(
        f"sandboxes/{sb.id}/workspace"
    )

    # sandbox terminal again, artifact advanced to continuations/2
    sb2 = _get(session_factory, Sandbox, sb.id)
    assert sb2.status == "done"
    assert sb2.terminal_reason == "agent_done"
    assert sb2.artifact_path is not None
    assert sb2.artifact_path.endswith("continuations/2")
    assert (Path(sb2.artifact_path) / "workspace").is_dir()
    assert (Path(sb2.artifact_path) / "transcript.log").is_file()
    # NEW artifact rows under the phase-2 path (prior dir untouched)
    with session_factory() as s:
        arts = list(
            s.execute(
                select(Artifact).where(Artifact.sandbox_id == sb.id)
            ).scalars()
        )
        assert arts
        assert all("continuations/2" in a.path for a in arts)

    # run back to terminal with a finished verdict
    r = _get(session_factory, Run, run.id)
    assert r.status == "done"
    assert r.finished_at is not None

    # evaluator re-scored ONLY this sandbox, on the phase-2 artifact dir
    assert len(calls) == 1
    assert calls[0]["sandbox_id"] == sb.id
    assert str(calls[0]["artifact_dir"]).endswith("continuations/2")
    assert calls[0]["gateway_cred"].startswith("heg.")

    # timeline: phase boundary checkpoints
    with session_factory() as s:
        stages = [
            e.payload.get("stage")
            for e in s.execute(select(Event).where(Event.run_id == run.id))
            .scalars()
            if e.type == "checkpoint"
        ]
    assert "continuation_completed" in stages
    assert "run_completed" in stages


async def test_continuation_eval_only_continued_sandbox(
    session_factory, settings, artifact_root
) -> None:
    """A second (untouched) sibling must not be re-scored."""
    run = make_run(session_factory, status="provisioning", config=run_config(
        harness_configs=[
            {"adapter": "miniloop", "skill_packs": []},
            {"adapter": "miniloop", "skill_packs": []},
        ]
    ))
    _approved_rubric(session_factory, run.id)
    continued = _continuation_sandbox(session_factory, run.id)
    # sibling already done with a phase-1 artifact
    with session_factory() as s:
        sib = Sandbox(
            run_id=run.id,
            harness_config={"adapter": "miniloop", "skill_packs": []},
            status="done",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            terminal_reason="agent_done",
            artifact_path=str(artifact_root / str(run.id) / "sib1"),
        )
        s.add(sib)
        s.commit()
        sibling_id = sib.id

    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)
    calls: list = []
    await run_continuation(
        run.id,
        continued.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )

    assert len(calls) == 1
    assert calls[0]["sandbox_id"] == continued.id
    assert calls[0]["sandbox_id"] != sibling_id


# ---------------------------------------------------------------------------
# budgets / monitor scoping
# ---------------------------------------------------------------------------


async def test_continuation_token_budget_is_per_phase(
    session_factory, settings, artifact_root
) -> None:
    """Phase-1 spend must not insta-trip the phase-2 token ceiling."""
    cfg = run_config(
        budgets={
            "max_iters": 10,
            "token_budget": 100_000,
            "wall_clock_seconds": 3600,
        }
    )
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)
    sb = _continuation_sandbox(session_factory, run.id)
    # prior phase already spent 150k — over the 100k run budget
    _add_event(
        session_factory, run.id, sb.id, "llm_response",
        {"usage": {"total_tokens": 150_000}},
    )

    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)
    calls: list = []
    await run_continuation(
        run.id,
        sb.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )

    sb2 = _get(session_factory, Sandbox, sb.id)
    # adjusted ceiling = 150k spent + 100k allowance -> phase completes
    assert sb2.terminal_reason == "agent_done"
    assert sb2.status == "done"


async def test_continuation_stale_done_signal_not_counted(
    session_factory, settings, artifact_root
) -> None:
    """Phase-1's done_signal must not terminate phase 2 early — and the
    running-state grace path must never see it (since-scoped lookup)."""
    run = make_run(session_factory, status="provisioning", config=run_config())
    _approved_rubric(session_factory, run.id)
    sb = _continuation_sandbox(session_factory, run.id)
    # old done_signal from phase 1 — far outside the 30s grace window
    _add_event(
        session_factory, run.id, sb.id, "done_signal",
        {"summary": "phase-1 done"},
        ts=datetime.now(UTC) - timedelta(hours=1),
    )

    calls = {"n": 0}

    def seq_status():
        calls["n"] += 1
        return (
            {"status": "running"}
            if calls["n"] == 1
            else {"status": "exited", "exit_code": 0}
        )

    fake = FakeRuntime(
        artifact_root=artifact_root,
        default_logs=RESULT_DONE,
        status_map={str(sb.id): seq_status},
    )
    calls_eval: list = []
    await run_continuation(
        run.id,
        sb.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls_eval),
    )

    sb2 = _get(session_factory, Sandbox, sb.id)
    assert sb2.status == "done"
    assert sb2.terminal_reason == "agent_done"
    # grace-stop would have called docker stop — the exited-verdict path
    # finalizes without it.
    assert str(sb.id) not in fake.stopped


async def test_continuation_stale_budget_error_not_counted(
    session_factory, settings, artifact_root
) -> None:
    """A phase-1 gateway 429 error event must not terminate phase 2."""
    run = make_run(session_factory, status="provisioning", config=run_config())
    _approved_rubric(session_factory, run.id)
    sb = _continuation_sandbox(session_factory, run.id)
    _add_event(
        session_factory, run.id, sb.id, "error",
        {"reason": "budget_exceeded", "http_status": 429},
        ts=datetime.now(UTC) - timedelta(hours=1),
    )

    calls = {"n": 0}

    def seq_status():
        calls["n"] += 1
        return (
            {"status": "running"}
            if calls["n"] <= 2
            else {"status": "exited", "exit_code": 0}
        )

    fake = FakeRuntime(
        artifact_root=artifact_root,
        default_logs=RESULT_DONE,
        status_map={str(sb.id): seq_status},
    )
    calls_eval: list = []
    await run_continuation(
        run.id,
        sb.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls_eval),
    )

    sb2 = _get(session_factory, Sandbox, sb.id)
    assert sb2.terminal_reason == "agent_done"
    assert str(sb.id) not in fake.stopped


# ---------------------------------------------------------------------------
# failure paths + run close
# ---------------------------------------------------------------------------


async def test_continuation_spawn_failure_keeps_prior_verdict(
    session_factory, settings, artifact_root
) -> None:
    """Respawn failure -> sandbox failed/error, no re-eval of the stale
    artifact, run closes on its existing scores."""
    run = make_run(session_factory, status="provisioning", config=run_config())
    _approved_rubric(session_factory, run.id)
    sb = _continuation_sandbox(
        session_factory, run.id, artifact_path="/nonexistent/phase1"
    )
    # prior score exists -> run verdict survives the failed phase
    with session_factory() as s:
        s.add(
            Score(sandbox_id=sb.id, rubric_id=None, dimension="d", score=8.0)
        )
        s.commit()

    fake = FakeRuntime(artifact_root=artifact_root, spawn_fail_ids={str(sb.id)})
    calls: list = []
    await run_continuation(
        run.id,
        sb.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )

    sb2 = _get(session_factory, Sandbox, sb.id)
    assert sb2.status == "failed"
    assert sb2.terminal_reason == "error"
    assert calls == []  # no fresh artifact -> no re-eval drift
    r = _get(session_factory, Run, run.id)
    assert r.status == "done"  # prior scores preserved the verdict


async def test_close_run_if_idle_waits_for_siblings(
    session_factory, settings, artifact_root
) -> None:
    """The run must stay active while ANY sandbox phase is still running."""
    run = make_run(session_factory, status="provisioning", config=run_config())
    sb_done = _continuation_sandbox(session_factory, run.id, status="done")
    # an unrelated still-running sandbox keeps the run open
    with session_factory() as s:
        active = Sandbox(
            run_id=run.id,
            harness_config={"adapter": "miniloop"},
            status="running",
            started_at=datetime.now(UTC),
        )
        s.add(active)
        s.add(
            Score(
                sandbox_id=sb_done.id, rubric_id=None, dimension="d", score=8.0
            )
        )
        s.commit()
        active_id = active.id

    assert not continuation.close_run_if_idle(session_factory, run.id)
    assert _get(session_factory, Run, run.id).status != "done"

    # sibling finishes -> next close lands the terminal verdict
    with session_factory() as s:
        row = s.get(Sandbox, active_id)
        row.status = "done"
        s.commit()
    assert continuation.close_run_if_idle(session_factory, run.id)
    r = _get(session_factory, Run, run.id)
    assert r.status == "done"
    assert r.finished_at is not None

    # idempotent — a second close no-ops on the terminal run
    assert continuation.close_run_if_idle(session_factory, run.id)


async def test_close_run_if_idle_failed_without_scores(
    session_factory, settings, artifact_root
) -> None:
    run = make_run(session_factory, status="provisioning", config=run_config())
    sb = _continuation_sandbox(session_factory, run.id, status="done")
    with session_factory() as s:
        row = s.get(Sandbox, sb.id)
        row.status = "done"
        s.commit()
    assert continuation.close_run_if_idle(session_factory, run.id)
    assert _get(session_factory, Run, run.id).status == "failed"


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_next_phase_and_wrap_task_prompt() -> None:
    assert continuation.next_phase(None) == 2
    assert continuation.next_phase({"continuations": []}) == 2
    assert continuation.next_phase(
        {"continuations": [{"phase": 2, "prompt": "x"}]}
    ) == 3

    wrapped = continuation.wrap_task_prompt("build 2048", "now add tests", 2)
    assert "build 2048" in wrapped
    assert "continuation — phase 2" in wrapped
    assert "now add tests" in wrapped
    assert wrapped.index("build 2048") < wrapped.index("now add tests")


async def test_run_continuation_missing_rows_noop(
    session_factory, settings, artifact_root, fake_runtime
) -> None:
    """run_continuation tolerates deleted run/sandbox rows (never raises)."""
    await run_continuation(
        uuid.uuid4(),
        uuid.uuid4(),
        session_factory=session_factory,
        runtime=fake_runtime,
        settings=settings,
        evaluator_fn=None,
    )


# ---------------------------------------------------------------------------
# restart reconciliation
# ---------------------------------------------------------------------------


async def test_startup_sweep_restores_verdict_after_interrupted_continuation(
    session_factory, settings, artifact_root, fake_runtime
) -> None:
    """An api restart mid-continuation must not regress a finished run: the
    sweep fails the in-flight continuation sandbox but restores the run's
    prior verdict (done — it scored in phase 1)."""
    from orchestrator import runner

    run = make_run(session_factory, status="running", config=run_config())
    sb = _continuation_sandbox(session_factory, run.id, status="running")
    with session_factory() as s:
        # phase-1 score against the same sandbox row — the run's real verdict
        s.add(
            Score(
                sandbox_id=sb.id,
                rubric_id=None,
                dimension="code_quality",
                score=8.0,
                rationale="ok",
            )
        )
        s.commit()

    await runner.startup_sweep(
        session_factory=session_factory,
        settings=settings,
        runtime=fake_runtime,
    )

    sb2 = _get(session_factory, Sandbox, sb.id)
    assert sb2.status == "failed"
    assert sb2.terminal_reason == "error"
    r = _get(session_factory, Run, run.id)
    assert r.status == "done"
    assert r.finished_at is not None

    # the sweep's checkpoint explains the restore, not a fresh failure
    with session_factory() as s:
        msgs = [
            e.payload.get("message", "")
            for e in s.execute(select(Event).where(Event.run_id == run.id))
            .scalars()
            if e.type == "error"
        ]
    assert any("continuation phase interrupted" in m for m in msgs)
