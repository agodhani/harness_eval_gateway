"""F9 — run analytics: per-sandbox metrics + side-by-side cost estimate.

``GET /api/runs/{run_id}/analytics?price_model=<name>`` aggregates the run's
``events`` rows per sandbox (volume is small — fetch + aggregate in Python):

- ``llm_turns``/``tokens``/``latency_ms`` from ``llm_response`` payloads
  (``usage.{prompt,completion,total}_tokens``, ``latency_ms`` — see
  ``gateway/metering.py::emit_llm_response``).
- ``tool_calls.by_tool`` merges every per-tool signal without double
  counting: ``tool_call`` events (miniloop, translated streams) plus
  hook-sourced ``tool_result`` outcomes (claude_code PostToolUse, pi's
  extension — per name the larger count wins since one call produces
  both records). Lanes with neither (codex ``command_execution``, aider's
  text stream) fall back to the coarse ``shell_command`` / ``file_op`` /
  ``test_execution`` counts as activity proxies so the mix is non-zero.
  ``skill_calls.by_skill`` keys on the invocation payload ``name``.
- plain counts for ``shell_command`` / ``file_op`` / ``test_execution`` /
  ``error``; ``wall_clock_seconds`` = max−min event ts for the sandbox.
- ``empty_workspace`` flags sandboxes whose monitor-side finalize found a
  workspace with no real files (``sandbox_finalized`` payload /
  ``stage="empty_workspace"`` warning — see orchestrator/monitor.py).
- ``estimated_cost_usd`` at the selected ``price_model`` (api/pricing.py —
  the ``model_prices`` glob table layered over the static ``PRICE_TABLE``,
  PR28); auto mode (no param) costs each sandbox at its own participant
  model's resolved rate. Unlisted models are **unpriced** (PR21):
  ``price_model``/``estimated_cost_usd`` come back null instead of being
  billed at another model's rate.

Evaluator-phase split: eval creds carry the *participant* sandbox_id (F7 —
judge events land on the participant timeline), so judge traffic would pollute
participant metrics. The first ``source="evaluator"`` event for a sandbox (the
``eval_started`` checkpoint, emitted before the judge spawns) marks the
boundary; everything at/after it is reported under ``evaluator`` instead.

F13 cross-run trends: ``GET /api/analytics/runs`` rolls the same participant/
evaluator split up to per-run points (tokens, turns, errors, mean overall
score, cost) for the most recent runs — powers the analysis page's trend
chart. ``?price_model=`` reprices every point uniformly; omitted, each point
is costed at its run's per-cell model rates — any unlisted cell model makes
the point's cost unpriced (null), never a fallback rate (PR21).
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import get_db
from api.pricing import (
    ModelPrice,
    estimate_cost_usd,
    load_book,
    resolve_price_model,
)
from api.schemas import (
    CellAnalytics,
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
from orchestrator.matrix import cell_key, run_models, sandbox_model, trial_index

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
        # Hook-sourced tool outcomes (claude_code PostToolUse, pi extension):
        # name -> count, folded into resolved_tool_calls below.
        self._tool_results: dict[str, int] = {}
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
            case "tool_result":
                # Outcome record for a call the stream may already have
                # logged — dedup happens per-name in resolved_tool_calls.
                name = str(
                    payload.get("name")
                    or payload.get("tool")
                    or payload.get("tool_name")
                    or "unknown"
                )
                self._tool_results[name] = self._tool_results.get(name, 0) + 1
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

    def resolved_tool_calls(self) -> ToolCallStats:
        """The sandbox's tool-call mix across every agent-activity signal.

        ``tool_call`` rows (miniloop, translated streams) are the base.
        ``tool_result`` rows (PR17 hooks) describe the SAME calls' outcomes,
        so per tool name the larger count wins — summing would double-count
        a call that produced both records (e.g. pi's ``bash`` appears as a
        ``shell_command`` at start and a ``tool_result`` at end). When a
        lane has neither (codex ``command_execution`` only, aider's text
        stream), the coarse shell/file/test events fold in as proxies so
        CLI adapters don't report an all-zero mix.
        """
        by_tool = dict(self.tool_calls.by_tool)
        for name, count in self._tool_results.items():
            by_tool[name] = max(by_tool.get(name, 0), count)
        if not by_tool:
            for label, count in (
                ("shell_command", self.shell_commands),
                ("file_op", self.file_ops),
                ("test_execution", self.test_executions),
            ):
                if count:
                    by_tool[label] = count
        return ToolCallStats(total=sum(by_tool.values()), by_tool=by_tool)


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


def _empty_workspace_sandboxes(events: list[Event]) -> set[UUID]:
    """Sandbox ids whose finalize flagged an empty workspace.

    Two monitor-emitted markers (orchestrator/monitor.py): the
    ``sandbox_finalized`` checkpoint's ``empty_workspace`` payload key and
    the dedicated ``stage="empty_workspace"`` warning — either stamps the
    flag on the analytics row. Collected phase-independently: the markers
    are orchestrator events, not participant traffic.
    """
    out: set[UUID] = set()
    for ev in events:
        if ev.sandbox_id is None:
            continue
        payload = ev.payload if isinstance(ev.payload, dict) else {}
        if payload.get("empty_workspace") or (
            ev.type == "checkpoint"
            and payload.get("stage") == "empty_workspace"
        ):
            out.add(ev.sandbox_id)
    return out


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
        "tool_calls": agg.resolved_tool_calls(),
        "skill_calls": agg.skill_calls,
        "shell_commands": agg.shell_commands,
        "file_ops": agg.file_ops,
        "test_executions": agg.test_executions,
        "errors": agg.errors,
        "wall_clock_seconds": round(agg.wall_clock_seconds(), 3),
    }


def _eval_activity(
    eval_agg: _Agg | None, eval_price: ModelPrice | None
) -> EvaluatorActivity | None:
    """Judge-phase rollup for one scope (sandbox or model), or None.

    ``eval_price`` is None when the evaluator model isn't in the price table
    — the cost reports null (unpriced), never a guessed rate.
    """
    if eval_agg is None:
        return None
    return EvaluatorActivity(
        llm_turns=eval_agg.llm_turns,
        tokens=eval_agg.tokens,
        errors=eval_agg.errors,
        estimated_cost_usd=(
            round(
                estimate_cost_usd(
                    eval_price,
                    input_tokens=eval_agg.tokens.input,
                    output_tokens=eval_agg.tokens.output,
                    cache_read_tokens=eval_agg.tokens.cache_read,
                ),
                6,
            )
            if eval_price is not None
            else None
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
    # participant model's listed rate. PR21: unlisted models are unpriced —
    # price_model/estimated_cost_usd come back null rather than billed at a
    # fallback rate. PR28: all resolution goes through the layered PriceBook —
    # a matching model_prices glob wins over the static table (and is itself
    # a valid explicit pick). Response price_model is the resolved explicit
    # key or None — mirrors RunTrends.
    book = load_book(session)
    explicit_key: str | None = None
    if price_model is not None:
        explicit_key = resolve_price_model(price_model, run.model_name, book)
        if explicit_key is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"unknown price_model {price_model!r}; "
                    f"known: {sorted(book.table())}"
                ),
            )

    def _price_key(model: str) -> str | None:
        if explicit_key is not None:
            return explicit_key
        return book.key_for(model)

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
    # PR27: cell identity per sandbox — every trial of one config × model
    # cell shares the key; the rollup groups on it for mean ± sd variance.
    sb_cell = {sb.id: cell_key(sb.harness_config or {}) for sb in sandboxes}

    # Eval boundary per sandbox (see _eval_boundaries).
    eval_start = _eval_boundaries(events)
    empty_ws = _empty_workspace_sandboxes(events)

    participant: dict[UUID, _Agg] = {sb.id: _Agg() for sb in sandboxes}
    evaluator: dict[UUID, _Agg] = {}
    # PR14: same events accumulated per participant model for the rollup.
    participant_by_model: dict[str, _Agg] = {}
    evaluator_by_model: dict[str, _Agg] = {}
    # PR27: per-cell accumulators for the trial-group rollup.
    participant_by_cell: dict[str, _Agg] = {}
    evaluator_by_cell: dict[str, _Agg] = {}
    for ev in events:
        if ev.sandbox_id is None or ev.sandbox_id not in participant:
            continue  # run-level rows / non-sandbox traffic
        model = sb_model[ev.sandbox_id]
        cell = sb_cell[ev.sandbox_id]
        if _is_eval_event(ev, eval_start.get(ev.sandbox_id)):
            evaluator.setdefault(ev.sandbox_id, _Agg()).add(ev)
            evaluator_by_model.setdefault(model, _Agg()).add(ev)
            evaluator_by_cell.setdefault(cell, _Agg()).add(ev)
        else:
            participant[ev.sandbox_id].add(ev)
            participant_by_model.setdefault(model, _Agg()).add(ev)
            participant_by_cell.setdefault(cell, _Agg()).add(ev)

    # Judge traffic is priced at the evaluator model's rate when known —
    # judge and participant models may differ (evaluator_model is opt-in).
    # Unlisted evaluator model → unpriced, except under an explicit
    # ?price_model= what-if reprice which applies the picked rate.
    cfg = run.config or {}
    evaluator_model = cfg.get("evaluator_model") or run.model_name
    eval_price = book.price_for(book.key_for(evaluator_model)) or (
        book.price_for(explicit_key) if explicit_key else None
    )

    out: list[SandboxAnalytics] = []
    for sb in sandboxes:
        agg = participant[sb.id]
        model = sb_model[sb.id]
        price_key = _price_key(model)
        price = book.price_for(price_key)
        out.append(
            SandboxAnalytics(
                sandbox_id=sb.id,
                name=sb.name,
                trial_index=trial_index(sb.harness_config),
                cell=sb_cell[sb.id],
                label=_sandbox_label(sb.harness_config or {}),
                harness_config=sb.harness_config or {},
                model=model,
                price_model=price_key,
                status=sb.status,
                # Row value wins; fall back to the harness-reported terminal
                # state event when the column wasn't updated.
                terminal_reason=sb.terminal_reason or agg.last_terminal_state,
                empty_workspace=sb.id in empty_ws,
                **_agg_metrics(agg),
                estimated_cost_usd=(
                    round(
                        estimate_cost_usd(
                            price,
                            input_tokens=agg.tokens.input,
                            output_tokens=agg.tokens.output,
                            cache_read_tokens=agg.tokens.cache_read,
                        ),
                        6,
                    )
                    if price is not None
                    else None
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
    participant_set = run_models(cfg, run.model_name)
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
        price = book.price_for(price_key)
        metrics = _agg_metrics(agg)
        metrics["wall_clock_seconds"] = model_wall_clock.get(model, 0.0)
        models_out.append(
            ModelAnalytics(
                model=model,
                price_model=price_key,
                sandbox_count=sum(1 for m in sb_model.values() if m == model),
                **metrics,
                estimated_cost_usd=(
                    round(
                        estimate_cost_usd(
                            price,
                            input_tokens=agg.tokens.input,
                            output_tokens=agg.tokens.output,
                            cache_read_tokens=agg.tokens.cache_read,
                        ),
                        6,
                    )
                    if price is not None
                    else None
                ),
                evaluator=_eval_activity(evaluator_by_model.get(model), eval_price),
            )
        )

    # PR27 — per-cell (config × model) rollups with score-variance columns.
    # Overall scores are newest-wins per sandbox (same ordering as the
    # results matrix); sd is the sample standard deviation (n−1), null when
    # fewer than two trials produced an overall score.
    overall_scores = (
        session.execute(
            select(Score.sandbox_id, Score.score)
            .where(
                Score.sandbox_id.in_([sb.id for sb in sandboxes]),
                Score.dimension == OVERALL_DIMENSION,
            )
            .order_by(
                Score.created_at.asc(),
                Score.seq.asc().nulls_first(),
                Score.rubric_version.asc().nulls_first(),
                Score.id.asc(),
            )
        )
        .all()
        if sandboxes
        else []
    )
    overall_by_sandbox: dict[UUID, float] = {}
    for sid, sc in overall_scores:
        overall_by_sandbox[sid] = sc  # ordered asc -> newest wins

    cell_order: list[str] = []
    cell_members: dict[str, list[Sandbox]] = {}
    for sb in sandboxes:
        cell = sb_cell[sb.id]
        if cell not in cell_members:
            cell_members[cell] = []
            cell_order.append(cell)
        cell_members[cell].append(sb)

    cells_out: list[CellAnalytics] = []
    for cell in cell_order:
        members = cell_members[cell]
        agg = participant_by_cell.get(cell, _Agg())
        first = members[0]
        model = sb_model[first.id]
        price_key = _price_key(model)
        price = book.price_for(price_key)
        metrics = _agg_metrics(agg)
        metrics["wall_clock_seconds"] = round(
            sum(participant[sb.id].wall_clock_seconds() for sb in members), 3
        )
        scores = [overall_by_sandbox[sb.id] for sb in members if sb.id in overall_by_sandbox]
        cells_out.append(
            CellAnalytics(
                cell=cell,
                label=_sandbox_label(first.harness_config or {}),
                model=model,
                sandbox_count=len(members),
                **metrics,
                estimated_cost_usd=(
                    round(
                        estimate_cost_usd(
                            price,
                            input_tokens=agg.tokens.input,
                            output_tokens=agg.tokens.output,
                            cache_read_tokens=agg.tokens.cache_read,
                        ),
                        6,
                    )
                    if price is not None
                    else None
                ),
                evaluator=_eval_activity(evaluator_by_cell.get(cell), eval_price),
                overall_mean=(
                    round(sum(scores) / len(scores), 3) if scores else None
                ),
                overall_sd=(
                    round(statistics.stdev(scores), 3) if len(scores) >= 2 else None
                ),
                overall_n=len(scores),
            )
        )

    return RunAnalytics(
        run_id=run.id,
        model_name=run.model_name,
        price_model=explicit_key,
        price_table=book.table(),
        price_globs=book.globs(),
        sandboxes=out,
        models=models_out,
        cells=cells_out,
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
    point is repriced uniformly; without it each point's participant cells
    are costed at their own model's listed rate — a cell on an unlisted
    model makes the whole point unpriced (PR21: a partial total would read
    as complete).
    """
    # PR28: explicit picks resolve through the layered book — a model_prices
    # glob is a valid ?price_model= and shadows the static key it covers.
    book = load_book(session)
    explicit_key: str | None = None
    if price_model is not None:
        explicit_key = resolve_price_model(price_model, book=book)
        if explicit_key is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"unknown price_model {price_model!r}; "
                    f"known: {sorted(book.table())}"
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

    run_by_id = {r.id: r for r in runs}
    sb_run: dict[UUID, UUID] = {sb.id: sb.run_id for sb in sandboxes}
    # PR14: each sandbox's participant model — participant cost is bucketed
    # by it so multi-model runs price each cell at its own rate.
    sb_model = {
        sb.id: sandbox_model(
            sb.harness_config or {}, run_by_id[sb.run_id].model_name
        )
        for sb in sandboxes
    }
    sb_count: dict[UUID, int] = {}
    for sb in sandboxes:
        sb_count[sb.run_id] = sb_count.get(sb.run_id, 0) + 1
    eval_start = _eval_boundaries(events)
    participant: dict[UUID, _Agg] = {r.id: _Agg() for r in runs}
    participant_by_model: dict[UUID, dict[str, _Agg]] = {}
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
            participant_by_model.setdefault(rid, {}).setdefault(
                sb_model[ev.sandbox_id], _Agg()
            ).add(ev)

    # Latest overall score per sandbox → mean per run (mirrors the results
    # endpoint's "rows ordered asc → newest wins" rule before averaging).
    sandbox_ids = list(sb_run)
    scores = (
        session.execute(
            select(Score)
            .where(Score.sandbox_id.in_(sandbox_ids))
            .order_by(
                Score.created_at.asc(),
                Score.seq.asc().nulls_first(),
                Score.id,
            )
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
        # resolved price — None when the run's model isn't listed (unpriced).
        selected = (
            explicit_key
            if explicit_key is not None
            else resolve_price_model(None, run.model_name, book)
        )
        # Judge traffic: the evaluator model's own rate when listed, else
        # the explicit pick (what-if reprice) — never another model's rate.
        eval_price = book.price_for(book.key_for(evaluator_model)) or (
            book.price_for(explicit_key) if explicit_key is not None else None
        )

        agg = participant[run.id]
        eval_agg = evaluator.get(run.id)
        overalls = overall_by_run.get(run.id, [])
        # PR14 per-model pricing: auto mode costs each participant-model
        # bucket at its own listed rate; an explicit ?price_model= reprices
        # every bucket at the one key. PR21: an unlisted bucket is unpriced —
        # its tokens get no rate and the point total reports null, since a
        # partial sum would read as the run's full cost. price_model is None
        # when auto pricing mixed rates — no single key honestly applies.
        cost = 0.0
        unpriced = False
        used_keys: set[str] = set()
        for model, mb in participant_by_model.get(run.id, {}).items():
            key = explicit_key or book.key_for(model)
            price = book.price_for(key)
            if price is None or key is None:
                unpriced = True
                continue
            used_keys.add(key)
            cost += estimate_cost_usd(
                price,
                input_tokens=mb.tokens.input,
                output_tokens=mb.tokens.output,
                cache_read_tokens=mb.tokens.cache_read,
            )
        if explicit_key is not None:
            point_price_model = explicit_key
        elif unpriced or len(used_keys) > 1:
            point_price_model = None
        elif used_keys:
            point_price_model = next(iter(used_keys))
        else:
            point_price_model = selected  # no participant traffic — $0 anyway
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
                price_model=point_price_model,
                estimated_cost_usd=None if unpriced else round(cost, 6),
                evaluator_cost_usd=(
                    round(
                        estimate_cost_usd(
                            eval_price,
                            input_tokens=eval_agg.tokens.input if eval_agg else 0,
                            output_tokens=eval_agg.tokens.output if eval_agg else 0,
                            cache_read_tokens=(
                                eval_agg.tokens.cache_read if eval_agg else 0
                            ),
                        ),
                        6,
                    )
                    if eval_price is not None
                    else None
                ),
                mean_overall_score=(
                    round(sum(overalls) / len(overalls), 2) if overalls else None
                ),
            )
        )

    return RunTrends(
        price_model=explicit_key,
        price_table=book.table(),
        points=points,
    )
