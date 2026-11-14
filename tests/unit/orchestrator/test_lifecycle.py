"""Lifecycle: provision -> monitor -> evaluate -> done/failed."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from db.models import Event, Rubric, Sandbox, Score
from orchestrator.lifecycle import run_lifecycle

from .conftest import FakeRuntime, make_run, run_config

DIMS = [
    "functional_requirements",
    "acceptance_criteria",
    "code_quality",
    "cyclomatic_complexity",
]

RESULT_DONE = "MINILOOP_RESULT " + json.dumps(
    {"terminal_state": "agent_done", "iters": 2}
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
                        dimension=d,
                        score=8.0,
                        rationale="looks fine",
                    )
                )
            s.commit()
        return []

    return fake_eval


def _lifecycles_settings(settings):
    return settings  # SimpleNamespace already carries everything needed


async def test_lifecycle_happy_path_two_sandboxes(
    session_factory, settings, artifact_root
) -> None:
    cfg = run_config(
        harness_configs=[
            {"adapter": "miniloop", "skill_packs": [], "system_prompt_extra": None},
            {"adapter": "miniloop", "skill_packs": ["pack-a"], "system_prompt_extra": "v2"},
        ]
    )
    run = make_run(session_factory, status="provisioning", config=cfg)
    rubric = _approved_rubric(session_factory, run.id)

    fake = FakeRuntime(
        artifact_root=artifact_root,
        logs_map={},
        default_logs=RESULT_DONE,
    )
    # per-sandbox logs keyed by uuid — populate after spawn? sandbox ids are
    # generated inside spawn_one; FakeRuntime.logs falls back to default_logs.
    calls: list = []
    evaluator = _score_writing_evaluator(calls)

    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=evaluator,
    )

    # 2 sandboxes provisioned + monitored to done
    with session_factory() as s:
        sbs = list(
            s.execute(select(Sandbox).where(Sandbox.run_id == run.id))
            .scalars()
            .all()
        )
        assert len(sbs) == 2
        assert all(sb.status == "done" for sb in sbs)
        assert all(sb.terminal_reason == "agent_done" for sb in sbs)
        assert all(sb.artifact_path for sb in sbs)
        r = s.get(type(run), run.id)
        assert r.status == "done"
        assert r.finished_at is not None
        # matrix expansion preserved config differences
        packs = sorted(sb.harness_config.get("skill_packs") or [] for sb in sbs)
        assert packs == [[], ["pack-a"]]
        extras = sorted(
            str(sb.harness_config.get("system_prompt_extra") or "") for sb in sbs
        )
        assert extras == ["", "v2"]
        # env differences reached the specs
        envs = [sp.env for sp in fake.spawned]
        assert any(e.get("SYSTEM_PROMPT_EXTRA") == "v2" for e in envs)
        assert any("SYSTEM_PROMPT_EXTRA" not in e for e in envs)
        # evaluator invoked once per sandbox with contract kwargs
        assert len(calls) == 2
        kw = calls[0]
        assert kw["rubric_id"] == rubric.id
        assert kw["model"] == "eval-model"
        assert kw["gateway_cred"].startswith("heg.")
        assert kw["gateway_base_url"] == settings.GATEWAY_URL
        assert kw["rubric"]["summary"] == "s"
        assert Path(kw["artifact_dir"]).is_dir()
        assert Path(kw["workspace_dir"]).is_dir()
        assert Path(kw["transcript_path"]).is_file()
        # score rows persisted by fake evaluator
        assert s.execute(select(Score).where(Score.rubric_id == rubric.id)).scalars().all()


async def test_lifecycle_partial_spawn_failure_still_done(
    session_factory, settings, artifact_root
) -> None:
    cfg = run_config(
        harness_configs=[
            {"adapter": "miniloop", "skill_packs": []},
            {"adapter": "miniloop", "skill_packs": []},
        ]
    )
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)

    fake = FakeRuntime(artifact_root=artifact_root, fail_first_spawn=True, default_logs=RESULT_DONE)
    calls: list = []
    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )

    with session_factory() as s:
        sbs = list(
            s.execute(select(Sandbox).where(Sandbox.run_id == run.id)).scalars().all()
        )
        assert len(sbs) == 2
        statuses = sorted(sb.status for sb in sbs)
        assert statuses == ["done", "failed"]
        r = s.get(type(run), run.id)
        assert r.status == "done"  # one sandbox scored -> run completes
        assert len(calls) == 1  # only the sandbox WITH an artifact is evaluated


async def test_lifecycle_all_spawns_fail_marks_run_failed(
    session_factory, settings, artifact_root
) -> None:
    cfg = run_config(
        harness_configs=[
            {"adapter": "miniloop", "skill_packs": []},
            {"adapter": "miniloop", "skill_packs": []},
        ]
    )
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)

    fake = FakeRuntime(artifact_root=artifact_root, fail_first_spawn=True)
    calls: list = []

    def always_fail(spec):
        raise RuntimeError("no docker for you")

    fake.spawn = always_fail  # type: ignore[method-assign]
    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )

    with session_factory() as s:
        r = s.get(type(run), run.id)
        assert r.status == "failed"
        assert r.finished_at is not None
        errs = [
            e for e in s.execute(select(Event).where(Event.run_id == run.id)).scalars()
            if e.type == "error"
        ]
        assert any(e.payload.get("stage") == "provisioning" for e in errs)
    assert calls == []


async def test_lifecycle_evaluator_failure_zero_scores_fails_run(
    session_factory, settings, artifact_root
) -> None:
    cfg = run_config()
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)

    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)

    async def boom_eval(**kwargs):
        raise RuntimeError("evaluator exploded")

    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=boom_eval,
    )

    with session_factory() as s:
        r = s.get(type(run), run.id)
        assert r.status == "failed"  # zero Score rows -> failed
        errs = [
            e for e in s.execute(select(Event).where(Event.run_id == run.id)).scalars()
            if e.type == "error" and e.payload.get("stage") == "evaluation"
        ]
        assert errs
        # sandbox itself still completed fine
        sb = s.execute(select(Sandbox).where(Sandbox.run_id == run.id)).scalar_one()
        assert sb.status == "done"
        assert sb.terminal_reason == "agent_done"


async def test_lifecycle_missing_evaluator_module_fails_gracefully(
    session_factory, settings, artifact_root, monkeypatch
) -> None:
    """evaluator_fn=None -> lazy import; simulate ImportError -> run failed."""
    cfg = run_config()
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)

    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)

    import orchestrator.lifecycle as lifecycle_mod

    def _no_evaluator():
        raise ImportError("evaluator not built yet")

    monkeypatch.setattr(lifecycle_mod, "_import_evaluator", _no_evaluator)
    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=None,
    )

    with session_factory() as s:
        r = s.get(type(run), run.id)
        assert r.status == "failed"
        errs = [
            e for e in s.execute(select(Event).where(Event.run_id == run.id)).scalars()
            if e.type == "error" and "evaluator unavailable" in str(e.payload)
        ]
        assert errs


async def test_lifecycle_eval_retry_recovers_empty_scores(
    session_factory, settings, artifact_root
) -> None:
    """Judge flakes once (no scores), retry scores -> run done + retry event."""
    cfg = run_config()
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)
    settings.EVAL_SCORE_RETRIES = 1

    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)
    calls: list = []

    async def flaky_eval(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return []  # judge produced no usable scores.json
        with kwargs["session_factory"]() as s:
            s.add(
                Score(
                    sandbox_id=kwargs["sandbox_id"],
                    rubric_id=kwargs["rubric_id"],
                    dimension="d",
                    score=8.0,
                    rationale="ok",
                )
            )
            s.commit()
        return []

    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=flaky_eval,
    )

    assert len(calls) == 2
    with session_factory() as s:
        assert s.get(type(run), run.id).status == "done"
        evs = s.execute(select(Event).where(Event.run_id == run.id)).scalars().all()
        retries = [
            e for e in evs
            if e.type == "checkpoint" and e.payload.get("stage") == "eval_retry"
        ]
        assert len(retries) == 1
        assert retries[0].payload["attempt"] == 2


async def test_lifecycle_eval_retry_exhausted_still_fails(
    session_factory, settings, artifact_root
) -> None:
    """Judge never produces scores -> bounded attempts, run still failed."""
    cfg = run_config()
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)
    settings.EVAL_SCORE_RETRIES = 1

    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)
    calls: list = []

    async def no_scores(**kwargs):
        calls.append(kwargs)
        return []

    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=no_scores,
    )

    assert len(calls) == 2  # initial attempt + 1 retry, then give up
    with session_factory() as s:
        assert s.get(type(run), run.id).status == "failed"


async def test_lifecycle_eval_exception_not_retried(
    session_factory, settings, artifact_root
) -> None:
    """Infra-level evaluator exceptions are deterministic — no retry."""
    cfg = run_config()
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)
    settings.EVAL_SCORE_RETRIES = 2

    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)
    calls: list = []

    async def boom_eval(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("spawn exploded")

    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=boom_eval,
    )

    assert len(calls) == 1
    with session_factory() as s:
        assert s.get(type(run), run.id).status == "failed"


async def test_lifecycle_no_approved_rubric_fails(
    session_factory, settings, artifact_root
) -> None:
    cfg = run_config()
    run = make_run(session_factory, status="provisioning", config=cfg)
    # NO rubric row at all
    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)
    calls: list = []
    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )
    with session_factory() as s:
        assert s.get(type(run), run.id).status == "failed"
    assert calls == []
