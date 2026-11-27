"""Lifecycle: provision -> monitor -> evaluate -> done/failed."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import httpx
from sqlalchemy import select

from db.models import Event, Rubric, Sandbox, Score
from orchestrator.lifecycle import run_lifecycle
from sandbox import seeds

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


async def test_lifecycle_multi_model_matrix_expansion(
    session_factory, settings, artifact_root
) -> None:
    """PR14: harness_configs × models → one sandbox per cell; each carries
    its model in harness_config, env MODEL_NAME, and the display name."""
    cfg = run_config(
        harness_configs=[
            {"adapter": "miniloop", "skill_packs": [], "name": "baseline"},
            {"adapter": "miniloop", "skill_packs": ["pack-a"], "name": "packed"},
        ]
    )
    # effective participant set — model_name leads (dedupe keeps order)
    cfg["models"] = ["test-model", "other-model"]
    run = make_run(
        session_factory, status="provisioning", config=cfg, model_name="test-model"
    )
    _approved_rubric(session_factory, run.id)

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
        sbs = list(
            s.execute(select(Sandbox).where(Sandbox.run_id == run.id))
            .scalars()
            .all()
        )
        # 2 configs × 2 models = 4 sandboxes, all completed
        assert len(sbs) == 4
        assert all(sb.status == "done" for sb in sbs)
        # every cell self-describes its model
        assert sorted(sb.harness_config.get("model") for sb in sbs) == [
            "other-model", "other-model", "test-model", "test-model",
        ]
        # names disambiguate same-config siblings across models
        assert sorted(sb.name for sb in sbs) == [
            "baseline · other-model",
            "baseline · test-model",
            "packed · other-model",
            "packed · test-model",
        ]
        assert s.get(type(run), run.id).status == "done"
        # evaluator ran once per cell
        assert len(calls) == 4

    # env MODEL_NAME reached each spawned spec with the cell's model
    env_models = sorted(sp.env["MODEL_NAME"] for sp in fake.spawned)
    assert env_models == ["other-model", "other-model", "test-model", "test-model"]


async def test_lifecycle_single_model_keeps_unexpanded_configs(
    session_factory, settings, artifact_root
) -> None:
    """PR14 regression: a run without config.models spawns exactly one
    sandbox per harness_config with NO model key injected (pre-PR14 shape)."""
    cfg = run_config(
        harness_configs=[
            {"adapter": "miniloop", "skill_packs": []},
            {"adapter": "miniloop", "skill_packs": ["pack-a"]},
        ]
    )
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)

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
        sbs = list(
            s.execute(select(Sandbox).where(Sandbox.run_id == run.id))
            .scalars()
            .all()
        )
        assert len(sbs) == 2
        assert all("model" not in (sb.harness_config or {}) for sb in sbs)
        # names have no model suffix on single-model runs
        assert all("·" not in (sb.name or "") for sb in sbs)
    assert {sp.env["MODEL_NAME"] for sp in fake.spawned} == {"test-model"}


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


async def test_lifecycle_eval_retry_feeds_prior_error(
    session_factory, settings, artifact_root
) -> None:
    """Error-feedback retry: attempt 2's prior_error carries the previous
    attempt's eval_scores_failed detail — feedback, not a blind respawn."""
    cfg = run_config()
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)
    settings.EVAL_SCORE_RETRIES = 1

    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)
    calls: list = []

    async def flaky_eval(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            # mimic evaluate_sandbox's eval_scores_failed error event
            with kwargs["session_factory"]() as s:
                s.add(
                    Event(
                        run_id=kwargs["run_id"],
                        sandbox_id=kwargs["sandbox_id"],
                        source="evaluator",
                        type="error",
                        payload={
                            "phase": "eval_scores_failed",
                            "reason": "scores_invalid",
                            "detail": "scores.json is not valid JSON",
                        },
                    )
                )
                s.commit()
            return []
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
    # first attempt: no feedback — nothing failed yet
    assert not calls[0].get("prior_error")
    prior = calls[1].get("prior_error")
    assert prior is not None
    assert "scores_invalid" in prior
    assert "not valid JSON" in prior


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
        # a final per-sandbox eval_failed error marks "gave up" (distinct
        # from "retry pending") — the results API derives eval_error from it.
        sb = s.execute(select(Sandbox).where(Sandbox.run_id == run.id)).scalar_one()
        failed = [
            e
            for e in s.execute(
                select(Event).where(
                    Event.run_id == run.id, Event.sandbox_id == sb.id
                )
            ).scalars()
            if e.type == "error" and e.payload.get("phase") == "eval_failed"
        ]
        assert len(failed) == 1
        assert failed[0].payload["attempts"] == 2


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
        evs = s.execute(select(Event).where(Event.run_id == run.id)).scalars().all()
        eval_failed = [
            e for e in evs
            if e.type == "error" and e.payload.get("phase") == "eval_failed"
        ]
        assert len(eval_failed) == 1  # finality marker after the infra break


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


# ---------------------------------------------------------------------------
# PR25 — prompt-only cells (zero-container matrix cells)
# ---------------------------------------------------------------------------


def _chat_ok(text: str = "cell output") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        },
    )


def _patch_gateway(monkeypatch, handler) -> list[httpx.Request]:
    """Swap prompt_cell's client factory for a MockTransport one — the cell
    builds its own httpx.AsyncClient, so patching the class attribute the
    module calls is the least invasive injection."""
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    real = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(recording)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    return requests


async def test_lifecycle_prompt_only_run(
    session_factory, settings, artifact_root, monkeypatch
) -> None:
    """The card's verify: a `prompt` x N-models run completes with ZERO
    docker containers, scores land, and the cell calls went through the
    (fake) gateway."""
    requests = _patch_gateway(monkeypatch, lambda r: _chat_ok())

    cfg = run_config(
        harness_configs=[{"adapter": "prompt", "skill_packs": []}]
    )
    cfg["models"] = ["m-a", "m-b", "m-c"]
    run = make_run(
        session_factory, status="provisioning", config=cfg, model_name="m-a"
    )
    _approved_rubric(session_factory, run.id)

    fake = FakeRuntime(artifact_root=artifact_root)
    calls: list = []
    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )

    # no docker: nothing spawned, nothing snapshotted by the monitor
    assert fake.spawned == []
    assert fake.snapshots == []
    # one gateway call per cell
    assert len(requests) == 3
    models = sorted(json.loads(r.content)["model"] for r in requests)
    assert models == ["m-a", "m-b", "m-c"]

    with session_factory() as s:
        sbs = list(
            s.execute(select(Sandbox).where(Sandbox.run_id == run.id)).scalars()
        )
        assert len(sbs) == 3
        assert all(sb.status == "done" for sb in sbs)
        assert all(sb.terminal_reason == "harness_complete" for sb in sbs)
        assert all(sb.docker_container_id is None for sb in sbs)
        assert all(
            (Path(sb.artifact_path) / "output.txt").read_text() == "cell output"
            for sb in sbs
        )
        r = s.get(type(run), run.id)
        assert r.status == "done"  # scores landed -> done
        # orchestrator-side cell events landed on the sandbox rows
        evs = s.execute(select(Event).where(Event.run_id == run.id)).scalars().all()
        assert sum(1 for e in evs if e.type == "run_started") == 3
        assert sum(1 for e in evs if e.type == "terminal_state") == 3
        # evaluator saw all three cells
        assert len(calls) == 3
        prompt_paths = sorted(c["prompt_output_path"] for c in calls)
        assert all(str(p).endswith("output.txt") for p in prompt_paths)


async def test_lifecycle_mixed_prompt_and_container(
    session_factory, settings, artifact_root, monkeypatch
) -> None:
    """A run mixing prompt and container cells routes each correctly —
    prompt cells finalize inline (no monitor), containers spawn + monitor
    as before — and both reach evaluation."""
    _patch_gateway(monkeypatch, lambda r: _chat_ok("prompt says hi"))

    cfg = run_config(
        harness_configs=[
            {"adapter": "prompt", "name": "prompted"},
            {"adapter": "miniloop", "name": "contained"},
        ]
    )
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)

    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)
    calls: list = []
    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )

    # exactly ONE spawn — the miniloop cell only
    assert len(fake.spawned) == 1
    with session_factory() as s:
        sbs = {
            (sb.harness_config or {}).get("adapter"): sb
            for sb in s.execute(
                select(Sandbox).where(Sandbox.run_id == run.id)
            ).scalars()
        }
        prompt_sb = sbs["prompt"]
        container_sb = sbs["miniloop"]
        assert prompt_sb.status == "done"
        assert prompt_sb.terminal_reason == "harness_complete"
        assert prompt_sb.docker_container_id is None
        assert container_sb.status == "done"
        assert container_sb.docker_container_id is not None
        assert s.get(type(run), run.id).status == "done"

    # both evaluated; only the prompt cell carries prompt_output_path
    by_adapter = {
        (c["sandbox_id"] == prompt_sb.id): c for c in calls
    }
    assert by_adapter[True]["prompt_output_path"] == Path(
        prompt_sb.artifact_path
    ) / "output.txt"
    assert by_adapter[False]["prompt_output_path"] is None
    # the prompt cell still gets the container kwargs shape (evaluator ignores ws)
    assert "workspace_dir" in by_adapter[True]


async def test_lifecycle_all_prompt_cells_fail_marks_run_failed(
    session_factory, settings, artifact_root, monkeypatch
) -> None:
    """Zero artifact-bearing cells -> run failed, same rule as containers.
    A non-httpx client failure leaves no artifact at all (prompt_cell's
    generic failure path)."""
    def boom(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("mock transport exploded")

    _patch_gateway(monkeypatch, boom)

    cfg = run_config(harness_configs=[{"adapter": "prompt"}])
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)

    fake = FakeRuntime(artifact_root=artifact_root)
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
        sb = s.execute(
            select(Sandbox).where(Sandbox.run_id == run.id)
        ).scalar_one()
        assert sb.status == "failed"
        assert sb.terminal_reason == "error"
    assert calls == []  # nothing to evaluate


# ---------------------------------------------------------------------------
# PR24 — run-level seed materialization
# ---------------------------------------------------------------------------

SEED_UPLOAD_ID = "12345678-1234-5678-9abc-def012345678"


def _stage_seed_upload(settings, upload_id: str = SEED_UPLOAD_ID) -> None:
    """Stage a real archive under the uploads root the run resolves — the
    same layout POST /api/uploads produces."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in (("proj/app.py", b"print(1)"), ("proj/README.md", b"r")):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    d = seeds.uploads_root(settings) / upload_id
    d.mkdir(parents=True)
    (d / f"{seeds.UPLOAD_ARCHIVE_NAME}.tar").write_bytes(buf.getvalue())
    (d / seeds.UPLOAD_META_NAME).write_text(
        json.dumps({"filename": "seed.tar", "size": 1, "sha256": "abc"})
    )


async def test_lifecycle_seed_failure_fails_run_fast(
    session_factory, settings, artifact_root
) -> None:
    """A permanently failing seed (dangling archive_id) fails the RUN once,
    before any cell — the per-cell path would retry the same materialization
    in every sequential spawn while the run holds the pacing slot."""
    cfg = run_config(
        harness_configs=[
            {"adapter": "miniloop", "name": "a"},
            {"adapter": "miniloop", "name": "b"},
        ]
    )
    cfg["task_inputs"] = {"archive_id": "00000000-0000-0000-0000-000000000000"}
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)

    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)
    calls: list = []
    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )

    assert fake.spawned == []
    with session_factory() as s:
        assert (
            s.execute(select(Sandbox).where(Sandbox.run_id == run.id))
            .scalars()
            .all()
            == []
        )
        r = s.get(type(run), run.id)
        assert r.status == "failed"
        assert r.finished_at is not None
        errs = [
            e
            for e in s.execute(select(Event).where(Event.run_id == run.id)).scalars()
            if e.type == "error"
        ]
        assert any(e.payload.get("stage") == "seed" for e in errs)
    assert calls == []


async def test_lifecycle_seed_materialized_once_for_all_cells(
    session_factory, settings, artifact_root, monkeypatch
) -> None:
    """The run's seed is prepared once at run level — both cells copy the
    same materialized tree (identical seed_sha on their rows)."""
    _stage_seed_upload(settings)
    cfg = run_config(
        harness_configs=[
            {"adapter": "miniloop", "name": "a"},
            {"adapter": "miniloop", "name": "b"},
        ]
    )
    cfg["task_inputs"] = {"archive_id": SEED_UPLOAD_ID}
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)

    real = seeds.prepare_run_seed
    preps = 0

    def counting(run_id, task_inputs, **kw):
        nonlocal preps
        preps += 1
        return real(run_id, task_inputs, **kw)

    monkeypatch.setattr(seeds, "prepare_run_seed", counting)

    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)
    calls: list = []
    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )

    assert preps == 1  # one materialization, N cells
    with session_factory() as s:
        sbs = list(
            s.execute(select(Sandbox).where(Sandbox.run_id == run.id)).scalars()
        )
        assert len(sbs) == 2
        assert all(sb.status == "done" for sb in sbs)
        shas = {sb.harness_config.get("seed_sha") for sb in sbs}
        assert len(shas) == 1 and None not in shas
    for spec in fake.spawned:
        assert (spec.workspace_host_path / "app.py").read_text() == "print(1)"


async def test_lifecycle_prompt_only_run_skips_seed(
    session_factory, settings, artifact_root, monkeypatch
) -> None:
    """Prompt cells have no workspace — task_inputs on a prompt-only run is
    inert: the seed is never fetched, so even a dangling one can't fail it."""
    requests = _patch_gateway(monkeypatch, lambda r: _chat_ok())

    cfg = run_config(harness_configs=[{"adapter": "prompt"}])
    cfg["task_inputs"] = {"archive_id": "00000000-0000-0000-0000-000000000000"}
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)

    fake = FakeRuntime(artifact_root=artifact_root)
    calls: list = []
    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )

    assert len(requests) == 1
    with session_factory() as s:
        assert s.get(type(run), run.id).status == "done"


# ---------------------------------------------------------------------------
# PR29 — judge panel
# ---------------------------------------------------------------------------


async def test_lifecycle_judge_panel_scores_every_artifact(
    session_factory, settings, artifact_root, monkeypatch
) -> None:
    """PR29 — config.evaluator_models = [j1, j2]: every judge evaluates
    every artifact on its own eval cred, tagged by model."""
    cfg = run_config()
    cfg["evaluator_models"] = ["judge-a", "judge-b"]
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)
    settings.EVAL_SCORE_RETRIES = 0
    settings.PROVIDER_ROUTES = '{"judge-b": "openrouter"}'

    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)
    calls: list = []
    evaluator = _score_writing_evaluator(calls)

    # mint_cred is re-entered per judge — its token payload has no judge
    # claim (run+sandbox+iat+exp+max_tokens only), so two mints in the same
    # second are byte-identical. "Own cred per judge" = a FRESH MINT inside
    # the judge loop (TTL + budget restart for every judge), which this
    # call count asserts.
    mints: list = []
    import orchestrator.lifecycle as lc

    orig_mint = lc.mint_cred

    def recording_mint(**kwargs):
        mints.append(kwargs)
        return orig_mint(**kwargs)

    monkeypatch.setattr(lc, "mint_cred", recording_mint)
    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=evaluator,
    )

    # 1 sandbox × 2 judges — panel order preserved, one fresh mint each
    assert [c["model"] for c in calls] == ["judge-a", "judge-b"]
    assert [c["local_judge"] for c in calls] == [True, False]
    assert len(mints) == 2
    with session_factory() as s:
        assert s.get(type(run), run.id).status == "done"
        rows = s.execute(select(Score)).scalars().all()
        assert len(rows) == 2 * len(DIMS)


async def test_lifecycle_judge_panel_failure_isolation(
    session_factory, settings, artifact_root
) -> None:
    """Judge A's hard failure can't zero judge B's scores or skip its pass —
    the per-judge eval_failed marker names the failed judge."""
    cfg = run_config()
    cfg["evaluator_models"] = ["judge-a", "judge-b"]
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)
    settings.EVAL_SCORE_RETRIES = 0

    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)
    calls: list = []

    async def judge_b_only(**kwargs):
        calls.append(kwargs)
        if kwargs["model"] == "judge-a":
            raise RuntimeError("judge-a exploded")
        with kwargs["session_factory"]() as s:
            s.add(
                Score(
                    sandbox_id=kwargs["sandbox_id"],
                    rubric_id=kwargs["rubric_id"],
                    dimension="d",
                    score=8.0,
                    rationale="ok",
                    judge_model="judge-b",
                )
            )
            s.commit()
        return []

    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=judge_b_only,
    )

    assert [c["model"] for c in calls] == ["judge-a", "judge-b"]
    with session_factory() as s:
        # judge-b's scores still landed → run completes done
        assert s.get(type(run), run.id).status == "done"
        evs = s.execute(select(Event).where(Event.run_id == run.id)).scalars().all()
        failed = [
            e for e in evs
            if e.type == "error" and e.payload.get("phase") == "eval_failed"
        ]
        assert len(failed) == 1
        assert failed[0].payload["judge_model"] == "judge-a"


async def test_lifecycle_no_panel_single_judge_compat(
    session_factory, settings, artifact_root
) -> None:
    """No evaluator_models → the single evaluator_model judges once —
    the pre-PR29 shape end to end."""
    cfg = run_config()
    run = make_run(session_factory, status="provisioning", config=cfg)
    _approved_rubric(session_factory, run.id)
    settings.EVAL_SCORE_RETRIES = 0

    fake = FakeRuntime(artifact_root=artifact_root, default_logs=RESULT_DONE)
    calls: list = []
    await run_lifecycle(
        run.id,
        session_factory=session_factory,
        runtime=fake,
        settings=settings,
        evaluator_fn=_score_writing_evaluator(calls),
    )
    assert [c["model"] for c in calls] == ["eval-model"]
