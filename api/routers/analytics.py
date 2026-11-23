"""F9 — run analytics: per-sandbox metrics + side-by-side cost estimate.

``GET /api/runs/{run_id}/analytics?price_model=<name>`` aggregates the run's
``events`` rows per sandbox (volume is small — fetch + aggregate in Python):

- ``llm_turns``/``tokens``/``latency_ms`` from ``llm_response`` payloads
  (``usage.{prompt,completion,total}_tokens``, ``latency_ms`` — see
  ``gateway/metering.py::emit_llm_response``).
- ``ttft_ms`` (avg/p95/max of the payload's first-byte timing) and
  ``avg_tokens_per_sec`` (mean per-call output throughput) — PR30 speed
  axis; events predating the fields contribute no sample. Cache replays
  are excluded from every timing stat (``latency_ms``, ``ttft_ms``,
  ``tokens_per_sec``) — a replay's timings measure the store's serve
  time, not the model (the gateway nulls ``tokens_per_sec`` on replays
  for the same reason).
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

PR32 cross-run leaderboard: ``GET /api/analytics/leaderboard`` pools the
newest-wins ``overall`` Score per sandbox across every run in scope into
two rankings — by participant model and by harness-config family
(``_family_key``: adapter + sorted skill-pack set) — with preset /
date-range / min-trials run-axis filters. Powers ``/leaderboard`` and the
preset-filtered embed on ``/presets/[slug]``.

PR36 dashboard summary: ``GET /api/analytics/summary`` aggregates the
non-leaderboard ``/dashboard`` cards in one call — an ISO-week series
(runs created + metered turns + est. spend, priced per participant /
judge / control-plane model rate — any unpriced call nulls that bucket's
cost, never a partial sum), the all-time terminal-reason mix, live
queue/active status, all-time totals, and the recent-runs table rows
(with mean ``overall``). ``?price_model=`` reprices every call uniformly,
same what-if convention as the trends endpoint.
"""

from __future__ import annotations

import math
import statistics
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
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
    DashboardQueue,
    DashboardQueueRun,
    DashboardRecentRun,
    DashboardSummary,
    DashboardTerminalSlice,
    DashboardTotals,
    DashboardWeekPoint,
    EvaluatorActivity,
    LatencyStats,
    Leaderboard,
    LeaderboardEntry,
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
        # PR30: per-call first-byte times + output throughput from the
        # gateway's speed fields (older events without them contribute no
        # sample — stats stay null rather than averaging phantom zeros).
        self.ttfts: list[int] = []
        self.toks_per_sec: list[float] = []
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
                cached = bool(payload.get("cached"))
                lat = _int(payload.get("latency_ms"))
                if lat > 0 and not cached:
                    self.latencies.append(lat)
                ttft = _int(payload.get("ttft_ms"))
                if ttft > 0 and not cached:
                    self.ttfts.append(ttft)
                tps = payload.get("tokens_per_sec")
                if (
                    not cached
                    and isinstance(tps, (int, float))
                    and not isinstance(tps, bool)
                    and math.isfinite(tps)
                    and tps >= 0
                ):
                    self.toks_per_sec.append(float(tps))
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
    ttft = LatencyStats(
        avg=round(sum(agg.ttfts) / len(agg.ttfts), 1) if agg.ttfts else None,
        p95=_p95(agg.ttfts) if agg.ttfts else None,
        max=float(max(agg.ttfts)) if agg.ttfts else None,
    )
    return {
        "llm_turns": agg.llm_turns,
        "tokens": agg.tokens,
        "latency_ms": lat,
        "ttft_ms": ttft,
        "avg_tokens_per_sec": (
            round(sum(agg.toks_per_sec) / len(agg.toks_per_sec), 1)
            if agg.toks_per_sec
            else None
        ),
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


# ---------------------------------------------------------------------------
# PR32 — cross-run leaderboard (GET /api/analytics/leaderboard)
# ---------------------------------------------------------------------------


def _family_key(harness_config: dict) -> tuple[str, str, str]:
    """(group key, label, adapter) for the config-family leaderboard.

    The family axis the PR32 card fixes: adapter + skill-pack set.
    ``model`` is deliberately excluded (the model board owns that axis),
    and so are PR1 config ``name``s and ``system_prompt_extra`` — a family
    is meant to pool the same harness *shape* across runs, not to reshard
    per-run tweaks. Named members still surface their shared name on the
    entry when the whole family agrees on one.
    """
    hc = harness_config or {}
    adapter = str(hc.get("adapter") or hc.get("harness") or "?")
    packs = sorted(str(p) for p in hc.get("skill_packs") or [])
    key = f"family:{adapter}|{','.join(packs)}"
    label = " · ".join([adapter, *packs])
    return key, label, adapter


def _leaderboard_board(raw: dict[str, dict], *, config: bool) -> list[LeaderboardEntry]:
    """Bucket map → ranked entries: mean ± sample sd (n−1, null under 2
    scored cells — a lone score has no spread to report)."""
    entries = [
        LeaderboardEntry(
            key=key,
            label=bucket.get("label") or key,
            adapter=bucket.get("adapter") if config else None,
            name=(
                next(iter(bucket["names"]))
                if config and len(bucket.get("names") or ()) == 1
                else None
            ),
            mean_overall_score=round(
                sum(bucket["scores"]) / len(bucket["scores"]), 2
            ),
            sd_overall_score=(
                round(statistics.stdev(bucket["scores"]), 3)
                if len(bucket["scores"]) >= 2
                else None
            ),
            n=len(bucket["scores"]),
            runs=len(bucket["runs"]),
            sandboxes=bucket["sandboxes"],
        )
        for key, bucket in raw.items()
        if bucket["scores"]  # unscored buckets have nothing to rank
    ]
    # Best first; deterministic ties (more evidence, then key) — same rule
    # as the preset leaderboard so boards read consistently.
    entries.sort(key=lambda e: (-e.mean_overall_score, -e.n, e.key))
    return entries


@router.get("/api/analytics/leaderboard", response_model=Leaderboard)
def get_leaderboard(
    session: DbSession,
    preset: Annotated[str | None, Query(max_length=64)] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    min_trials: Annotated[int, Query(ge=1)] = 1,
) -> Leaderboard:
    """Global rankings across every scored sandbox in scope.

    Buckets: participant model (``sandbox_model`` — a per-config ``model``
    pin wins over the run's ``model_name``, same as run analytics) and
    harness-config family (adapter + pack set, ``_family_key``). A
    sandbox's score is its LATEST ``overall`` row (asc order → newest
    wins, the results/trends merge rule) so rescored cells count once.

    Filters: ``preset`` pins ``runs.preset_ref``; ``since``/``until`` are
    inclusive bounds on ``runs.created_at`` (naive values read as UTC);
    ``min_trials`` keeps only runs submitted with ``trials >= N`` — the
    repeat-per-cell axis (``harness_config.trial_index`` members share
    their cell's family/model the same as single-shot cells).
    """
    # A date-only input parses naive — read it as UTC so the bound
    # compares and binds honestly against the timestamptz column on every
    # backend (a naive/aware ``>`` would itself raise TypeError).
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    if until is not None and until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    if since is not None and until is not None and since > until:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="since must be <= until",
        )

    stmt = select(Run).order_by(Run.created_at.desc(), Run.id)
    if preset:
        stmt = stmt.where(Run.preset_ref == preset)
    if since is not None:
        stmt = stmt.where(Run.created_at >= since)
    if until is not None:
        stmt = stmt.where(Run.created_at <= until)
    if min_trials > 1:
        stmt = stmt.where(Run.trials >= min_trials)
    runs = session.execute(stmt).scalars().all()
    run_by_id = {r.id: r for r in runs}

    sandboxes = (
        session.execute(
            select(Sandbox).where(Sandbox.run_id.in_(list(run_by_id)))
        )
        .scalars()
        .all()
        if run_by_id
        else []
    )

    latest: dict[UUID, float] = {}
    if sandboxes:
        rows = session.execute(
            select(Score.sandbox_id, Score.score)
            .where(
                Score.sandbox_id.in_([sb.id for sb in sandboxes]),
                Score.dimension == OVERALL_DIMENSION,
            )
            .order_by(
                Score.created_at.asc(),
                Score.seq.asc().nulls_first(),
                Score.id.asc(),
            )
        ).all()
        for sid, sc in rows:
            latest[sid] = sc  # ordered asc → newest wins

    by_model: dict[str, dict] = {}
    by_family: dict[str, dict] = {}
    for sb in sandboxes:
        run = run_by_id[sb.run_id]
        hc = sb.harness_config or {}
        score = latest.get(sb.id)

        model = sandbox_model(hc, run.model_name)
        mb = by_model.setdefault(
            model, {"scores": [], "runs": set(), "sandboxes": 0}
        )
        mb["sandboxes"] += 1
        if score is not None:
            mb["scores"].append(score)
            mb["runs"].add(sb.run_id)

        fkey, flabel, adapter = _family_key(hc)
        fb = by_family.setdefault(
            fkey,
            {
                "label": flabel,
                "adapter": adapter,
                "scores": [],
                "runs": set(),
                "sandboxes": 0,
                "names": set(),
            },
        )
        fb["sandboxes"] += 1
        name = str(hc.get("name") or "").strip()
        if name:
            fb["names"].add(name)
        if score is not None:
            fb["scores"].append(score)
            fb["runs"].add(sb.run_id)

    return Leaderboard(
        preset=preset,
        since=since,
        until=until,
        min_trials=min_trials,
        runs_count=len(runs),
        sandboxes_count=len(sandboxes),
        n_scored=len(latest),
        by_model=_leaderboard_board(by_model, config=False),
        by_config=_leaderboard_board(by_family, config=True),
    )


# ---------------------------------------------------------------------------
# PR36 — dashboard summary (GET /api/analytics/summary)
# ---------------------------------------------------------------------------

#: Run statuses the queue card reports as in-flight (pacing's single slot).
_ACTIVE_RUN_STATUSES = ("provisioning", "running", "evaluating")
#: Submitted-but-not-executing — parked before the pacing queue.
_AWAITING_RUN_STATUSES = ("submitted", "rubric_review")


def _utc(ts: datetime | None) -> datetime | None:
    """Aware-or-UTC — sqlite returns naive datetimes where Postgres yields
    timestamptz; naive values read as UTC (the leaderboard bound rule)."""
    if ts is not None and ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts


def _week_start(ts: datetime) -> datetime:
    """Monday 00:00 UTC of ``ts``'s ISO week."""
    ts = _utc(ts) or ts
    day = ts.date()
    monday = day - timedelta(days=day.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=UTC)


def _iso_week_label(week_start: datetime) -> str:
    iso = week_start.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


@router.get("/api/analytics/summary", response_model=DashboardSummary)
def get_dashboard_summary(
    session: DbSession,
    price_model: Annotated[str | None, Query()] = None,
    weeks: Annotated[int, Query(ge=1, le=52)] = 12,
    recent: Annotated[int, Query(ge=1, le=20)] = 8,
) -> DashboardSummary:
    """One aggregate for the /dashboard cards the leaderboard doesn't serve.

    ``weeks`` ISO buckets (oldest → current week) carry runs created, metered
    llm turns, and est. spend priced per call — participant traffic at the
    cell's own model rate, judge-phase traffic at the run's evaluator model,
    and run-level control-plane calls (rubric drafting — ``sandbox_id`` null)
    at the payload's recorded model. A call with no resolvable rate counts
    toward ``unpriced_llm_turns`` and nulls its bucket's cost total — the
    PR21 rule: a partial sum would read as the week's full spend.
    ``?price_model=`` reprices every call at one key (unknown → 422).

    ``terminal_mix`` is the all-time outcome histogram (pending lanes counted
    separately); ``queue`` reports the pacing slot live; ``recent_runs``
    mirrors the runs list (newest-first) with a mean-``overall`` column
    (newest-wins per sandbox, averaged — same merge rule as trends).
    """
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
        session.execute(select(Run).order_by(Run.created_at.desc(), Run.id))
        .scalars()
        .all()
    )
    run_by_id = {r.id: r for r in runs}
    sandboxes = session.execute(select(Sandbox)).scalars().all()
    sb_run: dict[UUID, UUID] = {sb.id: sb.run_id for sb in sandboxes}
    sb_model: dict[UUID, str] = {
        sb.id: sandbox_model(
            sb.harness_config or {}, run_by_id[sb.run_id].model_name
        )
        for sb in sandboxes
        if sb.run_id in run_by_id
    }
    # Judge boundary per sandbox — eval creds share the participant
    # sandbox_id, so source="evaluator" (or ts at/after its first eval
    # event) prices that call at the evaluator model, not the cell's.
    eval_start = {
        sid: ts
        for sid, ts in session.execute(
            select(Event.sandbox_id, func.min(Event.ts))
            .where(
                Event.sandbox_id.is_not(None),
                Event.source == "evaluator",
            )
            .group_by(Event.sandbox_id)
        ).all()
        if sid is not None and ts is not None
    }

    # Week buckets: ``weeks`` consecutive ISO weeks ending at the current
    # one (oldest first) — zero-filled weeks are honest (no runs/traffic).
    now = datetime.now(UTC)
    first_week = _week_start(now) - timedelta(weeks=weeks - 1)
    buckets = [first_week + timedelta(weeks=i) for i in range(weeks)]
    bucket_index = {ws: i for i, ws in enumerate(buckets)}
    week_runs = [0] * weeks
    week_turns = [0] * weeks
    week_cost = [0.0] * weeks
    week_unpriced = [0] * weeks
    week_has_unpriced = [False] * weeks

    for run in runs:
        if run.created_at is None:
            continue
        idx = bucket_index.get(_week_start(run.created_at))
        if idx is not None:
            week_runs[idx] += 1

    totals = DashboardTotals(runs=len(runs), sandboxes=len(sandboxes))
    total_cost = 0.0
    metered = (
        session.execute(select(Event).where(Event.type == "llm_response"))
        .scalars()
        .all()
    )
    for ev in metered:
        totals.llm_turns += 1
        t = _event_tokens(ev.payload if isinstance(ev.payload, dict) else {})
        totals.tokens.input += t.input
        totals.tokens.output += t.output
        totals.tokens.cache_read += t.cache_read
        totals.tokens.total += t.total

        run = run_by_id.get(ev.run_id)
        price: ModelPrice | None = None
        if explicit_key is not None:
            price = book.price_for(explicit_key)
        elif ev.sandbox_id is not None and ev.sandbox_id in sb_run:
            if _is_eval_event(ev, eval_start.get(ev.sandbox_id)):
                eval_model = (
                    (run.config or {}).get("evaluator_model") or run.model_name
                ) if run is not None else None
                price = book.price_for(book.key_for(eval_model))
            else:
                price = book.price_for(book.key_for(sb_model.get(ev.sandbox_id)))
        else:
            # Run-level traffic (rubric drafting on a sandbox-free cred) —
            # price at the model the payload recorded.
            payload = ev.payload if isinstance(ev.payload, dict) else {}
            model = payload.get("model")
            price = book.price_for(
                book.key_for(model if isinstance(model, str) else None)
            )

        idx = (
            bucket_index.get(_week_start(ev.ts))
            if ev.ts is not None
            else None
        )
        if price is None:
            totals.unpriced_llm_turns += 1
            if idx is not None:
                week_unpriced[idx] += 1
                week_has_unpriced[idx] = True
        else:
            cost = estimate_cost_usd(
                price,
                input_tokens=t.input,
                output_tokens=t.output,
                cache_read_tokens=t.cache_read,
            )
            total_cost += cost
            if idx is not None:
                week_cost[idx] += cost
        if idx is not None:
            week_turns[idx] += 1

    totals.estimated_cost_usd = (
        None if totals.unpriced_llm_turns > 0 else round(total_cost, 6)
    )
    n_scored = session.execute(
        select(func.count(func.distinct(Score.sandbox_id))).where(
            Score.dimension == OVERALL_DIMENSION
        )
    ).scalar_one()
    totals.scored_sandboxes = int(n_scored or 0)

    # Terminal mix — all-time, pending lanes reported separately.
    mix_counts: dict[str, int] = {}
    terminal_pending = 0
    for sb in sandboxes:
        if sb.terminal_reason:
            mix_counts[sb.terminal_reason] = (
                mix_counts.get(sb.terminal_reason, 0) + 1
            )
        else:
            terminal_pending += 1
    terminal_mix = [
        DashboardTerminalSlice(reason=reason, count=count)
        for reason, count in sorted(
            mix_counts.items(), key=lambda kv: (-kv[1], kv[0])
        )
    ]

    # Queue card — the pacing lane live: in-flight runs (should be ≤1),
    # parked runs in promotion order (same ordering claim_next_queued and
    # _queue_positions use: started_at asc nulls-last, created_at asc), and
    # the pre-queue crowd awaiting rubric review.
    active = [
        DashboardQueueRun(
            run_id=r.id,
            status=r.status,
            task_prompt=r.task_prompt[:120],
            model_name=r.model_name,
            preset_ref=r.preset_ref,
            created_at=r.created_at,
            started_at=r.started_at,
        )
        for r in runs
        if r.status in _ACTIVE_RUN_STATUSES
    ]
    queued_ids = (
        session.execute(
            select(Run.id)
            .where(Run.status == "queued")
            .order_by(Run.started_at.asc().nulls_last(), Run.created_at.asc())
        )
        .scalars()
        .all()
    )
    queued: list[DashboardQueueRun] = []
    for pos, qid in enumerate(queued_ids, start=1):
        qr = run_by_id.get(qid)
        if qr is None:
            continue
        queued.append(
            DashboardQueueRun(
                run_id=qr.id,
                status=qr.status,
                task_prompt=qr.task_prompt[:120],
                model_name=qr.model_name,
                preset_ref=qr.preset_ref,
                queue_position=pos,
                created_at=qr.created_at,
                started_at=qr.started_at,
            )
        )
    awaiting = sum(1 for r in runs if r.status in _AWAITING_RUN_STATUSES)

    # Recent runs + mean newest-wins overall (same score merge as trends:
    # asc (created_at, seq, rubric_version, id) → newest per sandbox wins,
    # then averaged over the run's scored cells).
    recent_runs = runs[:recent]
    recent_ids = {r.id for r in recent_runs}
    sb_count = {rid: 0 for rid in recent_ids}
    recent_sb_ids = [
        sb.id for sb in sandboxes if sb.run_id in recent_ids
    ]
    for sb in sandboxes:
        if sb.run_id in recent_ids:
            sb_count[sb.run_id] += 1
    latest_overall: dict[UUID, float] = {}
    if recent_sb_ids:
        for sid, sc in session.execute(
            select(Score.sandbox_id, Score.score)
            .where(
                Score.sandbox_id.in_(recent_sb_ids),
                Score.dimension == OVERALL_DIMENSION,
            )
            .order_by(
                Score.created_at.asc(),
                Score.seq.asc().nulls_first(),
                Score.rubric_version.asc().nulls_first(),
                Score.id.asc(),
            )
        ).all():
            latest_overall[sid] = sc
    overall_by_run: dict[UUID, list[float]] = {}
    for sb in sandboxes:
        if sb.run_id in recent_ids and sb.id in latest_overall:
            overall_by_run.setdefault(sb.run_id, []).append(
                latest_overall[sb.id]
            )
    recent_out = [
        DashboardRecentRun(
            run_id=r.id,
            status=r.status,
            task_prompt=r.task_prompt[:120],
            model_name=r.model_name,
            preset_ref=r.preset_ref,
            sandbox_count=sb_count.get(r.id, 0),
            created_at=r.created_at,
            finished_at=r.finished_at,
            mean_overall_score=(
                round(
                    sum(overall_by_run[r.id]) / len(overall_by_run[r.id]), 2
                )
                if overall_by_run.get(r.id)
                else None
            ),
        )
        for r in recent_runs
    ]

    return DashboardSummary(
        generated_at=now,
        price_model=explicit_key,
        totals=totals,
        weeks=[
            DashboardWeekPoint(
                week=_iso_week_label(ws),
                week_start=ws,
                runs=week_runs[i],
                llm_turns=week_turns[i],
                estimated_cost_usd=(
                    None if week_has_unpriced[i] else round(week_cost[i], 6)
                ),
                unpriced_llm_turns=week_unpriced[i],
            )
            for i, ws in enumerate(buckets)
        ],
        terminal_mix=terminal_mix,
        terminal_pending=terminal_pending,
        queue=DashboardQueue(
            active=active, queued=queued, awaiting_review=awaiting
        ),
        recent_runs=recent_out,
    )
