"""F9 — run analytics: per-sandbox metrics + side-by-side cost estimate.

``GET /api/runs/{run_id}/analytics?price_model=<name>`` aggregates the run's
``events`` rows per sandbox (volume is small — fetch + aggregate in Python):

- ``llm_turns``/``tokens``/``latency_ms`` from ``llm_response`` payloads
  (``usage.{prompt,completion,total}_tokens``, ``latency_ms`` — see
  ``gateway/metering.py::emit_llm_response``).
- ``tool_calls.by_tool`` / ``skill_calls.by_skill`` keyed on the miniloop
  payload ``name`` (with fallbacks for other emitters).
- plain counts for ``shell_command`` / ``file_op`` / ``test_execution`` /
  ``error``; ``wall_clock_seconds`` = max−min event ts for the sandbox.
- ``estimated_cost_usd`` at the selected ``price_model`` (api/pricing.py
  ``PRICE_TABLE``); default = the run's model when priced, else first entry.

Evaluator-phase split: eval creds carry the *participant* sandbox_id (F7 —
judge events land on the participant timeline), so judge traffic would pollute
participant metrics. The first ``source="evaluator"`` event for a sandbox (the
``eval_started`` checkpoint, emitted before the judge spawns) marks the
boundary; everything at/after it is reported under ``evaluator`` instead.

F13 cross-run trends: ``GET /api/analytics/runs`` rolls the same participant/
evaluator split up to per-run points (tokens, turns, errors, mean overall
score, cost) for the most recent runs — powers the analysis page's trend
chart. ``?price_model=`` reprices every point uniformly; omitted, each point
is costed at its own run's resolved price model.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import get_db
from api.pricing import PRICE_TABLE, estimate_cost_usd, resolve_price_model
from api.schemas import (
    EvaluatorActivity,
    LatencyStats,
    ModelAnalytics,
    RunAnalytics,
    RunTrendPoint,
    RunTrends,
    SandboxAnalytics,
    SkillCallStats,
    TokenUsage,
    ToolCallStats,
)
from db.models import Event, Run, Sandbox, Score
from evaluator.scores import OVERALL_DIMENSION
from orchestrator.matrix import participant_models, sandbox_model

router = APIRouter(tags=["analytics"])

DbSession = Annotated[Session, Depends(get_db)]


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _p95(values: list[int]) -> float:
    """Nearest-rank p95 — cheap and good enough at event volumes we see."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)])


def _event_tokens(payload: dict) -> TokenUsage:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return TokenUsage()
    inp = _int(usage.get("prompt_tokens"))
    out = _int(usage.get("completion_tokens"))
    total = _int(usage.get("total_tokens")) or inp + out
    # Anthropic-style cache fields, when a provider reports them (defensive —
    # the Ollama path doesn't emit cache stats today).
    cache = _int(usage.get("cache_read_input_tokens")) or _int(
        usage.get("cache_read_tokens")
    )
    return TokenUsage(input=inp, output=out, cache_read=cache, total=total)


class _Agg:
    """Running accumulator for one phase (participant or evaluator)."""

    def __init__(self) -> None:
        self.llm_turns = 0
        self.tokens = TokenUsage()
        self.latencies: list[int] = []
        self.tool_calls = ToolCallStats()
        self.skill_calls = SkillCallStats()
        self.shell_commands = 0
        self.file_ops = 0
        self.test_executions = 0
        self.errors = 0
        self.first_ts = None
        self.last_ts = None
        self.last_terminal_state: str | None = None

    def add(self, ev: Event) -> None:
        ts = ev.ts
        if ts is not None:
            if self.first_ts is None or ts < self.first_ts:
                self.first_ts = ts
            if self.last_ts is None or ts > self.last_ts:
                self.last_ts = ts
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        match ev.type:
            case "llm_response":
                self.llm_turns += 1
                t = _event_tokens(payload)
                self.tokens.input += t.input
                self.tokens.output += t.output
                self.tokens.cache_read += t.cache_read
                self.tokens.total += t.total
                lat = _int(payload.get("latency_ms"))
                if lat:
                    self.latencies.append(lat)
            case "tool_call":
                self.tool_calls.total += 1
                name = str(
                    payload.get("name") or payload.get("tool") or "unknown"
                )
                self.tool_calls.by_tool[name] = (
                    self.tool_calls.by_tool.get(name, 0) + 1
                )
            case "skill_invocation":
                self.skill_calls.total += 1
                name = str(
                    payload.get("name")
                    or payload.get("skill")
                    or payload.get("skill_name")
                    or "unknown"
                )
                self.skill_calls.by_skill[name] = (
                    self.skill_calls.by_skill.get(name, 0) + 1
                )
            case "shell_command":
                self.shell_commands += 1
            case "file_op":
                self.file_ops += 1
            case "test_execution":
                self.test_executions += 1
            case "error":
                self.errors += 1
            case "terminal_state" | "miniloop_result":
                state = payload.get("terminal_state")
                if isinstance(state, str) and state:
                    self.last_terminal_state = state

    def wall_clock_seconds(self) -> float:
        if self.first_ts is None or self.last_ts is None:
            return 0.0
        return max(0.0, (self.last_ts - self.first_ts).total_seconds())


def _eval_boundaries(events: list[Event]) -> dict[UUID, datetime]:
    """First evaluator-sourced event ts per sandbox — the judge boundary.

    The ``eval_started`` checkpoint precedes the judge container spawn, so any
    event at/after it (incl. cred-stamped gateway/harness rows) is judge
    activity, not participant activity.
    """
    boundary: dict[UUID, datetime] = {}
    for ev in events:
        if ev.sandbox_id is None or ev.source != "evaluator":
            continue
        prev = boundary.get(ev.sandbox_id)
        if ev.ts is not None and (prev is None or ev.ts < prev):
            boundary[ev.sandbox_id] = ev.ts
    return boundary


def _is_eval_event(ev: Event, boundary: datetime | None) -> bool:
    """True when the event belongs to the judge phase, not the participant."""
    return ev.source == "evaluator" or (
        boundary is not None and ev.ts is not None and ev.ts >= boundary
    )


def _agg_metrics(agg: _Agg) -> dict:
    """Metric fields shared by SandboxAnalytics and ModelAnalytics."""
    lat = LatencyStats(
        avg=round(sum(agg.latencies) / len(agg.latencies), 1)
        if agg.latencies
        else None,
        p95=_p95(agg.latencies) if agg.latencies else None,
        max=float(max(agg.latencies)) if agg.latencies else None,
    )
    return {
        "llm_turns": agg.llm_turns,
        "tokens": agg.tokens,
        "latency_ms": lat,
        "tool_calls": agg.tool_calls,
        "skill_calls": agg.skill_calls,
        "shell_commands": agg.shell_commands,
        "file_ops": agg.file_ops,
        "test_executions": agg.test_executions,
        "errors": agg.errors,
        "wall_clock_seconds": round(agg.wall_clock_seconds(), 3),
    }


def _eval_activity(eval_agg: _Agg | None, eval_price) -> EvaluatorActivity | None:
    """Judge-phase rollup for one scope (sandbox or model), or None."""
    if eval_agg is None:
        return None
    return EvaluatorActivity(
        llm_turns=eval_agg.llm_turns,
        tokens=eval_agg.tokens,
        errors=eval_agg.errors,
        estimated_cost_usd=round(
            estimate_cost_usd(
                eval_price,
                input_tokens=eval_agg.tokens.input,
                output_tokens=eval_agg.tokens.output,
            ),
            6,
        ),
    )


def _sandbox_label(harness_config: dict) -> str:
    """Mirror the results-matrix header: ``adapter · model · pack1, pack2``.

    The model segment only appears on multi-model cells (PR14 — the
    ``model`` key is injected by matrix expansion); single-model runs keep
    the pre-PR14 ``adapter · packs`` label.
    """
    hc = harness_config or {}
    adapter = str(hc.get("adapter") or hc.get("harness") or "?")
    model = str(hc.get("model") or "").strip()
    parts = [adapter]
    if model:
        parts.append(model)
    packs = hc.get("skill_packs")
    if isinstance(packs, list) and packs:
        parts.append(", ".join(str(p) for p in packs))
    return " · ".join(parts)


@router.get("/api/runs/{run_id}/analytics", response_model=RunAnalytics)
def get_run_analytics(
    run_id: UUID,
    session: DbSession,
    price_model: Annotated[str | None, Query()] = None,
) -> RunAnalytics:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")

    # PR14 pricing: an explicit ?price_model= reprices EVERY sandbox uniformly
    # (what-if view); omitted → auto mode, each sandbox costed at its OWN
    # participant model's listed rate (unlisted models fall back to the run's
    # resolved rate). Response price_model is the explicit key or None —
    # mirrors RunTrends.
    explicit_key: str | None = None
    if price_model is not None:
        explicit_key = resolve_price_model(price_model, run.model_name)
        if explicit_key is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"unknown price_model {price_model!r}; "
                    f"known: {sorted(PRICE_TABLE)}"
                ),
            )
    # Fallback rate for unlisted participant models — the run's own model
    # when priced, else the first table entry (never None for a None param).
    run_price_key = resolve_price_model(None, run.model_name)
    assert run_price_key is not None

    def _price_key(model: str) -> str:
        if explicit_key is not None:
            return explicit_key
        return model if model in PRICE_TABLE else run_price_key

    sandboxes = (
        session.execute(
            select(Sandbox)
            .where(Sandbox.run_id == run_id)
            .order_by(Sandbox.started_at.asc().nulls_last(), Sandbox.id)
        )
        .scalars()
        .all()
    )
    events = (
        session.execute(
            select(Event).where(Event.run_id == run_id).order_by(Event.id)
        )
        .scalars()
        .all()
    )

    # PR14: participant model per sandbox (config ``model`` cell else
    # run.model_name) — drives labels, per-sandbox pricing, and the rollup.
    sb_model = {
        sb.id: sandbox_model(sb.harness_config or {}, run.model_name)
        for sb in sandboxes
    }

    # Eval boundary per sandbox (see _eval_boundaries).
    eval_start = _eval_boundaries(events)

    participant: dict[UUID, _Agg] = {sb.id: _Agg() for sb in sandboxes}
    evaluator: dict[UUID, _Agg] = {}
    # PR14: same events accumulated per participant model for the rollup.
    participant_by_model: dict[str, _Agg] = {}
    evaluator_by_model: dict[str, _Agg] = {}
    for ev in events:
        if ev.sandbox_id is None or ev.sandbox_id not in participant:
            continue  # run-level rows / non-sandbox traffic
        model = sb_model[ev.sandbox_id]
        if _is_eval_event(ev, eval_start.get(ev.sandbox_id)):
            evaluator.setdefault(ev.sandbox_id, _Agg()).add(ev)
            evaluator_by_model.setdefault(model, _Agg()).add(ev)
        else:
            participant[ev.sandbox_id].add(ev)
            participant_by_model.setdefault(model, _Agg()).add(ev)

    # Judge traffic is priced at the evaluator model's rate when known —
    # judge and participant models may differ (evaluator_model is opt-in).
    cfg = run.config or {}
    evaluator_model = cfg.get("evaluator_model") or run.model_name
    eval_fallback = explicit_key or run_price_key
    eval_price = PRICE_TABLE.get(evaluator_model, PRICE_TABLE[eval_fallback])

    out: list[SandboxAnalytics] = []
    for sb in sandboxes:
        agg = participant[sb.id]
        model = sb_model[sb.id]
        price_key = _price_key(model)
        price = PRICE_TABLE[price_key]
        out.append(
            SandboxAnalytics(
                sandbox_id=sb.id,
                name=sb.name,
                label=_sandbox_label(sb.harness_config or {}),
                harness_config=sb.harness_config or {},
                model=model,
                price_model=price_key,
                status=sb.status,
                # Row value wins; fall back to the harness-reported terminal
                # state event when the column wasn't updated.
                terminal_reason=sb.terminal_reason or agg.last_terminal_state,
                **_agg_metrics(agg),
                estimated_cost_usd=round(
                    estimate_cost_usd(
                        price,
                        input_tokens=agg.tokens.input,
                        output_tokens=agg.tokens.output,
                    ),
                    6,
                ),
                evaluator=_eval_activity(evaluator.get(sb.id), eval_price),
            )
        )

    # PR14 compare-by-model rollup: one ModelAnalytics per participant model.
    # Order follows the run's participant set; sandbox-only models (legacy
    # rows whose config predates config.models) append in first-seen order.
    seen_models: list[str] = []
    for sb in sandboxes:
        m = sb_model[sb.id]
        if m not in seen_models:
            seen_models.append(m)
    participant_set = participant_models(run.model_name, cfg.get("models"))
    ordered_models = [m for m in participant_set if m in seen_models]
    ordered_models += [m for m in seen_models if m not in ordered_models]

    # Per-model wall clock sums member sandboxes' spans (parallel cells — a
    # pooled event span would collapse to the longest cell, not the total).
    model_wall_clock: dict[str, float] = {}
    for sb in sandboxes:
        m = sb_model[sb.id]
        model_wall_clock[m] = model_wall_clock.get(m, 0.0) + round(
            participant[sb.id].wall_clock_seconds(), 3
        )

    models_out: list[ModelAnalytics] = []
    for model in ordered_models:
        agg = participant_by_model.get(model, _Agg())
        price_key = _price_key(model)
        price = PRICE_TABLE[price_key]
        metrics = _agg_metrics(agg)
        metrics["wall_clock_seconds"] = model_wall_clock.get(model, 0.0)
        models_out.append(
            ModelAnalytics(
                model=model,
                price_model=price_key,
                sandbox_count=sum(1 for m in sb_model.values() if m == model),
                **metrics,
                estimated_cost_usd=round(
                    estimate_cost_usd(
                        price,
                        input_tokens=agg.tokens.input,
                        output_tokens=agg.tokens.output,
                    ),
                    6,
                ),
                evaluator=_eval_activity(evaluator_by_model.get(model), eval_price),
            )
        )

    return RunAnalytics(
        run_id=run.id,
        model_name=run.model_name,
        price_model=explicit_key,
        price_table=PRICE_TABLE,
        sandboxes=out,
        models=models_out,
    )


@router.get("/api/analytics/runs", response_model=RunTrends)
def get_run_trends(
    session: DbSession,
    price_model: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> RunTrends:
    """Cross-run trend points for the analysis page (F13).

    Newest-first: the ``limit`` most recent runs, each rolled up to
    participant + evaluator token/turn/error totals, the mean of its
    sandboxes' ``overall`` score rows, and cost. With ``price_model`` every
    point is repriced uniformly; without it each point is costed at its own
    run's resolved price (run's model when listed, else the table fallback).
    """
    if price_model is not None and price_model not in PRICE_TABLE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"unknown price_model {price_model!r}; "
                f"known: {sorted(PRICE_TABLE)}"
            ),
        )

    runs = (
        session.execute(
            select(Run).order_by(Run.created_at.desc(), Run.id).limit(limit)
        )
        .scalars()
        .all()
    )
    run_ids = [r.id for r in runs]
    sandboxes = (
        session.execute(select(Sandbox).where(Sandbox.run_id.in_(run_ids)))
        .scalars()
        .all()
        if run_ids
        else []
    )
    events = (
        session.execute(
            select(Event).where(Event.run_id.in_(run_ids)).order_by(Event.id)
        )
        .scalars()
        .all()
        if run_ids
        else []
    )

    sb_run: dict[UUID, UUID] = {sb.id: sb.run_id for sb in sandboxes}
    sb_count: dict[UUID, int] = {}
    for sb in sandboxes:
        sb_count[sb.run_id] = sb_count.get(sb.run_id, 0) + 1
    eval_start = _eval_boundaries(events)
    participant: dict[UUID, _Agg] = {r.id: _Agg() for r in runs}
    evaluator: dict[UUID, _Agg] = {}
    for ev in events:
        if ev.sandbox_id is None:
            continue
        rid = sb_run.get(ev.sandbox_id)
        if rid is None:
            continue  # run-level rows / non-sandbox traffic
        if _is_eval_event(ev, eval_start.get(ev.sandbox_id)):
            evaluator.setdefault(rid, _Agg()).add(ev)
        else:
            participant[rid].add(ev)

    # Latest overall score per sandbox → mean per run (mirrors the results
    # endpoint's "rows ordered asc → newest wins" rule before averaging).
    sandbox_ids = list(sb_run)
    scores = (
        session.execute(
            select(Score)
            .where(Score.sandbox_id.in_(sandbox_ids))
            .order_by(Score.created_at.asc(), Score.id)
        )
        .scalars()
        .all()
        if sandbox_ids
        else []
    )
    latest_overall: dict[UUID, float] = {}
    for sc in scores:
        if sc.dimension == OVERALL_DIMENSION:
            latest_overall[sc.sandbox_id] = sc.score
    overall_by_run: dict[UUID, list[float]] = {}
    for sb_id, score_val in latest_overall.items():
        overall_by_run.setdefault(sb_run[sb_id], []).append(score_val)

    points: list[RunTrendPoint] = []
    for run in runs:
        cfg = run.config or {}
        evaluator_model = cfg.get("evaluator_model") or run.model_name
        # Explicit param reprices every run uniformly; else the run's own
        # resolved price (never None for a None param — falls back to the
        # first table entry).
        selected = (
            price_model
            if price_model is not None
            else resolve_price_model(None, run.model_name)
        )
        assert selected is not None  # resolve_price_model only Nones on bad param
        price = PRICE_TABLE[selected]
        eval_price = PRICE_TABLE.get(evaluator_model, price)

        agg = participant[run.id]
        eval_agg = evaluator.get(run.id)
        overalls = overall_by_run.get(run.id, [])
        points.append(
            RunTrendPoint(
                run_id=run.id,
                status=run.status,
                task_prompt=run.task_prompt[:120],
                model_name=run.model_name,
                evaluator_model=evaluator_model,
                created_at=run.created_at,
                finished_at=run.finished_at,
                sandbox_count=sb_count.get(run.id, 0),
                llm_turns=agg.llm_turns,
                tokens=agg.tokens,
                errors=agg.errors,
                evaluator_llm_turns=eval_agg.llm_turns if eval_agg else 0,
                evaluator_tokens=(
                    eval_agg.tokens if eval_agg is not None else TokenUsage()
                ),
                evaluator_errors=eval_agg.errors if eval_agg else 0,
                price_model=selected,
                estimated_cost_usd=round(
                    estimate_cost_usd(
                        price,
                        input_tokens=agg.tokens.input,
                        output_tokens=agg.tokens.output,
                    ),
                    6,
                ),
                evaluator_cost_usd=round(
                    estimate_cost_usd(
                        eval_price,
                        input_tokens=eval_agg.tokens.input if eval_agg else 0,
                        output_tokens=eval_agg.tokens.output if eval_agg else 0,
                    ),
                    6,
                ),
                mean_overall_score=(
                    round(sum(overalls) / len(overalls), 2) if overalls else None
                ),
            )
        )

    return RunTrends(
        price_model=price_model,
        price_table=PRICE_TABLE,
        points=points,
    )
