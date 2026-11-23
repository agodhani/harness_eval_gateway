"""Pydantic request/response schemas for the control-plane API.

F6 adds the event-ingestion/query schemas (enums mirror db/models.py column
docs). Run-spec schemas (F1) and results schemas (F8) land with their routers.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from api.pricing import ModelPrice

# Re-exported so the control-plane score payloads live in one module — the
# results API embeds AnalysisReport on SandboxResult (see bottom section).
from scoring.models import AnalysisReport  # noqa: F401


class EventSource(str, Enum):
    """Who produced the event (db Event.source)."""

    GATEWAY = "gateway"
    HARNESS = "harness"
    SANDBOX = "sandbox"
    EVALUATOR = "evaluator"
    ORCHESTRATOR = "orchestrator"


class EventType(str, Enum):
    """Event kind (db Event.type).

    Ingest endpoints reject anything not listed here, so this must cover
    every type any producer can emit — gateway metering plus harness adapter
    output (contract-tested against adapters/miniloop's emit literals in
    tests/unit/api/test_event_types.py).
    """

    LLM_REQUEST = "llm_request"
    LLM_RESPONSE = "llm_response"
    TOOL_CALL = "tool_call"
    # tool_result is hook/plugin-sourced (PR17): harnesses with a native hook
    # surface (claude_code settings.json hooks, pi extension forwarder) report
    # tool OUTCOMES here — the adapter-translated streams deliberately skip
    # result records to avoid double-counting tool_call/shell_command/file_op.
    TOOL_RESULT = "tool_result"
    SHELL_COMMAND = "shell_command"
    FILE_OP = "file_op"
    TEST_EXECUTION = "test_execution"
    CHECKPOINT = "checkpoint"
    SKILL_INVOCATION = "skill_invocation"
    DONE_SIGNAL = "done_signal"
    RUN_STARTED = "run_started"
    TERMINAL_STATE = "terminal_state"
    CONTEXT_COMPACTED = "context_compacted"
    MINILOOP_RESULT = "miniloop_result"
    ERROR = "error"


class IngestEvent(BaseModel):
    """One event in an ingestion batch.

    `id`/`ts` are server-assigned; `ts` may be supplied by the producer
    (e.g. harness-native timestamps) and is used verbatim when present.
    `seq` is an optional ordering hint passed through unchanged.
    """

    run_id: uuid.UUID
    sandbox_id: uuid.UUID | None = None
    source: EventSource
    type: EventType
    seq: int | None = None
    ts: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class IngestRequest(BaseModel):
    """Batch envelope: {"events": [...]}.

    The ingest endpoint also accepts a bare IngestEvent object (single-event
    shorthand) via a union type — see api/routers/events.py.
    """

    events: list[IngestEvent] = Field(min_length=1)


class IngestResponse(BaseModel):
    accepted: int


class EventOut(BaseModel):
    """Event as returned by the query + SSE endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: uuid.UUID
    sandbox_id: uuid.UUID | None
    ts: datetime
    seq: int | None
    source: str
    type: str
    payload: dict[str, Any]


# ---------------------------------------------------------------------------
# F1/F2 — run spec + rubric schemas (api/routers/runs.py)
# ---------------------------------------------------------------------------


class HarnessConfigIn(BaseModel):
    """One sandbox configuration in a run's comparison matrix.

    ``adapter`` picks the harness adapter — ``miniloop`` plus the Phase 2.5
    CLI harnesses (``codex``, ``claude_code``, ``pi``, ``deepseek``,
    ``aider``; see ``orchestrator/adapters.py`` and ``GET /api/harnesses``).
    ``skill_packs``
    selects which vendored packs under ``./skillpacks`` are staged into the
    sandbox, and ``system_prompt_extra`` appends run-specific instructions
    (native ``--append-system-prompt`` where the harness has one, otherwise
    inlined into the task prompt footer — another comparison axis).

    ``name`` is an optional human label persisted onto the sandbox row
    (``sandboxes.name``) so same-adapter configs are distinguishable in the
    timeline filter, results matrix, and analytics. Blank → the provisioner
    assigns ``<adapter>-<sandbox short id>``.

    ``model`` optionally pins this config to a single participant model,
    overriding the run's model list for this config: the matrix expands to
    exactly one cell at the pinned model instead of one per listed model.
    """

    adapter: str = "miniloop"
    skill_packs: list[str] = Field(default_factory=list, max_length=16)
    system_prompt_extra: str | None = Field(default=None, max_length=8000)
    name: str | None = Field(default=None, max_length=64)
    model: str | None = Field(default=None, max_length=255)

    @field_validator("name", "model", mode="before")
    @classmethod
    def _normalize_optional_text(cls, v: object) -> object:
        """Collapse whitespace; empty → None (provisioner defaults the name)."""
        if isinstance(v, str):
            v = " ".join(v.split())
            return v or None
        return v


class BudgetsIn(BaseModel):
    """Termination-policy knobs for a run.

    ``token_budget`` is PER-RUN (the gateway meters tokens across all of a
    run's sandboxes against one ceiling), not per-sandbox.
    """

    max_iters: int = Field(default=50, ge=1, le=1000)
    token_budget: int = Field(default=200_000, ge=1_000)
    wall_clock_seconds: int = Field(default=3600, ge=60, le=86_400)


#: PR14 — sanity cap on the expanded comparison matrix (configs × models);
#: each cell is a real sandbox container.
MAX_MATRIX_CELLS = 32


class RunCreate(BaseModel):
    """POST /api/runs body — one evaluation submission."""

    # strip_whitespace applies BEFORE length checks — a whitespace-only
    # prompt/model 422s instead of persisting a spec rubric-gen can't use.
    task_prompt: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        min_length=1, max_length=100_000
    )
    # PR22: optional task-preset reference (a slug from GET /api/presets).
    # When set, the submit resolves the preset and REPLACES task_prompt with
    # the preset's body verbatim — preset_ref asserts the task came from the
    # preset, so a divergent prompt must be submitted without the ref (the
    # "edit as custom copy" path drops the ref rather than lying about
    # provenance). runs.preset_revision snapshots the resolved revision.
    preset_ref: Annotated[str, StringConstraints(strip_whitespace=True)] | None = Field(
        default=None, max_length=64
    )
    model_name: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        min_length=1, max_length=255
    )
    # PR14 model-vs-model: ADDITIONAL participant models — the matrix expands
    # to harness_configs × ([model_name] + models). Omitted/empty → the run
    # is single-model on model_name (pre-PR14 shape). A harness config's own
    # ``model`` pin overrides this list for that config.
    models: (
        list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]]
        | None
    ) = Field(default=None, max_length=8)
    harness_configs: list[HarnessConfigIn] = Field(min_length=1, max_length=8)
    # Defaults to model_name at submit (same-model eval is flagged, not banned).
    evaluator_model: Annotated[str, StringConstraints(strip_whitespace=True)] | None = (
        Field(default=None, max_length=255)
    )
    budgets: BudgetsIn = Field(default_factory=BudgetsIn)

    @model_validator(mode="after")
    def _normalize_models(self) -> RunCreate:
        """Dedupe/blank-drop ``models`` (model_name always wins), then cap
        the expanded matrix — 8 configs × 8 models would spawn 64 sandboxes.
        A config pinned via ``model`` expands to one cell, not len(models)."""
        if self.preset_ref is not None and not self.preset_ref:
            self.preset_ref = None
        if self.models is not None:
            seen = {self.model_name}
            cleaned: list[str] = []
            for m in self.models:
                if m and m not in seen:
                    seen.add(m)
                    cleaned.append(m)
            self.models = cleaned or None
        model_count = 1 + len(self.models or [])
        pinned = sum(1 for hc in self.harness_configs if hc.model)
        cells = pinned + (len(self.harness_configs) - pinned) * model_count
        if cells > MAX_MATRIX_CELLS:
            raise ValueError(
                f"run matrix would spawn {cells} sandboxes "
                f"({len(self.harness_configs)} configs × "
                f"{model_count} models); "
                f"max is {MAX_MATRIX_CELLS}"
            )
        return self


class SandboxOut(BaseModel):
    """One sandbox row as embedded in run responses."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    harness_config: dict[str, Any]
    status: str
    terminal_reason: str | None
    artifact_path: str | None
    started_at: datetime | None
    finished_at: datetime | None
    # Optional config label (PR1) — ``<adapter>-<shortid>`` default assigned
    # at provisioning; NULL on sandboxes that predate the column.
    name: str | None = None


class RubricOut(BaseModel):
    """A rubric row (draft or frozen/approved)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    version: int
    # PR5: version chain — the rubric row this version was edited from.
    parent_id: uuid.UUID | None = None
    status: str
    content: dict[str, Any]
    created_at: datetime


class RunOut(BaseModel):
    """Full run detail: spec + status + sandboxes + latest rubric."""

    id: uuid.UUID
    status: str
    task_prompt: str
    model_name: str
    config: dict[str, Any]
    # True when the evaluator model equals ANY participant model — the UI
    # flags this so users notice non-independent judging.
    same_model_eval: bool
    created_at: datetime
    updated_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    # PR22 preset provenance — the preset slug the task_prompt was resolved
    # from (null for freeform prompts) + the revision snapshot at submit.
    preset_ref: str | None = None
    preset_revision: int | None = None
    # PR26 suite provenance — set on runs fanned out by
    # POST /api/suites/{slug}/run: suite slug, the task's index inside
    # suite.yaml, and the shared suite_runs batch id.
    suite_ref: str | None = None
    suite_index: int | None = None
    suite_run_id: uuid.UUID | None = None
    # 1-based position among ``queued`` runs (approval/queue-entry order) —
    # set only while the run is parked; null in every other status.
    queue_position: int | None = None
    # Submit-time advisory warnings from the model-catalog check (e.g.
    # "model not in provider catalog") — populated on POST /api/runs only;
    # empty on reads (warnings are advisory, not persisted state).
    warnings: list[str] = Field(default_factory=list)
    sandboxes: list[SandboxOut] = Field(default_factory=list)
    rubric: RubricOut | None = None


class RunSummary(BaseModel):
    """One row of GET /api/runs — list view, no nested detail."""

    id: uuid.UUID
    status: str
    task_prompt: str  # truncated to 200 chars
    model_name: str
    # PR14: full participant-model set ([model_name] + config extras + any
    # per-config ``model`` pins) so the list can flag multi-model runs
    # without parsing ``config``.
    models: list[str] = Field(default_factory=list)
    created_at: datetime
    finished_at: datetime | None
    sandbox_count: int
    # PR22: preset slug the task came from (null for freeform prompts) —
    # drives the list view's preset column/filter.
    preset_ref: str | None = None
    # PR26: suite slug the run was fanned out from (+ its task index and
    # batch id) — drives the list view's suite filter/badge.
    suite_ref: str | None = None
    suite_index: int | None = None
    suite_run_id: uuid.UUID | None = None
    # Same contract as RunOut.queue_position — null unless status=queued.
    queue_position: int | None = None


class RunDeleteOut(BaseModel):
    """DELETE /api/runs/{id} outcome.

    ``cancelled`` — a pre-spawn run (submitted/rubric_review/queued) was
    marked cancelled; ``deleted`` — a terminal run and its rows were
    hard-removed. In-flight phases (provisioning/running/evaluating) 409.
    """

    status: Literal["cancelled", "deleted"]


# ---------------------------------------------------------------------------
# F11 — skillpack import (api/routers/skillpacks.py)
# ---------------------------------------------------------------------------


class SkillPackOut(BaseModel):
    """GET /api/skillpacks item — one selectable pack (vendored or imported)."""

    name: str
    source: str  # "vendored" | "imported"
    skill_count: int
    skills: list[str] = Field(default_factory=list)
    origin_url: str | None = None
    imported_at: str | None = None


class SkillpackDiscoverIn(BaseModel):
    """POST /api/skillpacks/discover body — a GitHub link to scan."""

    url: str = Field(min_length=1, max_length=2000)


class DiscoveredSkill(BaseModel):
    """One SKILL.md found in the repo — ``dir`` is the selection token."""

    dir: str
    name: str
    description: str = ""
    file_count: int = 0
    has_frontmatter: bool = False


class SkillpackDiscoverOut(BaseModel):
    """POST /api/skillpacks/discover response — repo scan result."""

    url: str  # canonical github URL actually scanned
    repo: str  # "owner/repo"
    ref: str  # resolved ref — reuse as import's ``ref`` pin
    commit_sha: str | None = None
    subpath: str  # scanned subtree ("" = repo root); file path for blob links
    kind: str  # root | tree | blob
    skills: list[DiscoveredSkill] = Field(default_factory=list)


class SkillpackImportIn(BaseModel):
    """POST /api/skillpacks/import body — persist a discover-selection.

    ``mode`` selects the target layout:

    - ``"pack"`` — all selected skills land in ONE pack named ``pack_name``.
      If the name is already taken (vendored or imported) the request fails
      with a 409 whose ``detail`` is ``{"kind": "name_conflict", "name",
      "suggested_name", "source"}`` — resend with ``pack_name`` set to the
      suggested/edited name (the confirm-and-rename flow).
    - ``"merge"`` — skills are appended into the existing *imported* pack
      ``pack_name`` (created when absent). Vendored targets are rejected:
      appending to vendored packs is never allowed.
    - ``"split"`` — every selected skill becomes its own pack named after
      the skill dir; colliding names are auto-renamed (``x`` → ``x-2``)
      and reported in the response. ``pack_name`` is ignored.
    """

    url: str = Field(min_length=1, max_length=2000)
    # Pin the discovered tree: resolved ref or commit_sha from the discover
    # response. Pair with ``subpath`` when pinning a sha for a branch whose
    # name contains slashes (its segment count differs from the URL's ref).
    ref: str | None = Field(default=None, max_length=255)
    subpath: str | None = Field(default=None, max_length=1000)
    # Repo-relative skill dirs (``dir`` values from /discover) to import.
    skills: list[str] = Field(min_length=1, max_length=64)
    pack_name: str = Field(default="", max_length=64)
    mode: Literal["pack", "merge", "split"] = "pack"

    @model_validator(mode="after")
    def _pack_name_required(self) -> SkillpackImportIn:
        if self.mode in ("pack", "merge") and not self.pack_name.strip():
            raise ValueError(f"pack_name is required for mode={self.mode!r}")
        return self


class ImportedSkill(BaseModel):
    """One skill actually written to disk."""

    source_dir: str  # repo-relative dir it came from
    name: str  # frontmatter name (dir fallback)
    dir: str  # final dir under <pack>/skills/
    file_count: int
    renamed: bool  # dir name was deduplicated (-2, -3…)


class PackImportResult(BaseModel):
    """One pack created/merged by an import call."""

    pack: str
    source: str = "imported"
    skill_count: int  # total skills in the pack post-write (merges append)
    imported: list[ImportedSkill] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class SkillpackImportOut(BaseModel):
    """POST /api/skillpacks/import response — one entry per pack touched
    (length 1 for ``pack``/``merge``, one per skill for ``split``)."""

    packs: list[PackImportResult]


# ---------------------------------------------------------------------------
# F9 — run analytics (api/routers/analytics.py)
# ---------------------------------------------------------------------------


class TokenUsage(BaseModel):
    """Aggregated token counts from a sandbox's llm_response events."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    total: int = 0


class LatencyStats(BaseModel):
    """LLM response latency stats in ms; all null when no llm_responses."""

    avg: float | None = None
    p95: float | None = None
    max: float | None = None


class ToolCallStats(BaseModel):
    total: int = 0
    by_tool: dict[str, int] = Field(default_factory=dict)


class SkillCallStats(BaseModel):
    total: int = 0
    by_skill: dict[str, int] = Field(default_factory=dict)


class EvaluatorActivity(BaseModel):
    """Post-run judge activity attributed to this sandbox's event stream.

    Eval creds carry the *participant* sandbox_id (so judge events land on the
    same timeline), so judge traffic is partitioned out at the evaluator's
    ``eval_started`` checkpoint and reported separately — otherwise the judge
    (up to ~30 iters) would swamp the participant's llm_turns/tokens.
    """

    llm_turns: int = 0
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    errors: int = 0
    estimated_cost_usd: float = 0.0


class SandboxAnalytics(BaseModel):
    """Per-sandbox metrics for the analytics comparison table."""

    sandbox_id: uuid.UUID
    # Config label from the sandbox row (PR1 named harness configs).
    name: str | None = None
    # Human label mirroring the results-matrix header (adapter · packs;
    # includes the model on multi-model runs — PR14).
    label: str = ""
    harness_config: dict[str, Any] = Field(default_factory=dict)
    # PR14: the participant model this sandbox ran (config ``model`` cell,
    # else the run's ``model_name``) + the price-table key its
    # ``estimated_cost_usd`` was computed at.
    model: str = ""
    price_model: str = ""
    status: str
    terminal_reason: str | None = None
    # Monitor flagged a done-ish terminal state on an empty workspace
    # (agent claimed completion but produced no files) — see the
    # ``stage="empty_workspace"`` warning / ``sandbox_finalized`` payload.
    empty_workspace: bool = False
    llm_turns: int = 0
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: LatencyStats = Field(default_factory=LatencyStats)
    tool_calls: ToolCallStats = Field(default_factory=ToolCallStats)
    skill_calls: SkillCallStats = Field(default_factory=SkillCallStats)
    shell_commands: int = 0
    file_ops: int = 0
    test_executions: int = 0
    errors: int = 0
    wall_clock_seconds: float = 0.0
    estimated_cost_usd: float = 0.0
    evaluator: EvaluatorActivity | None = None


class ModelAnalytics(BaseModel):
    """PR14 — per-model rollup of a run's sandboxes (compare-by-model).

    Same metric fields as ``SandboxAnalytics`` aggregated across every
    sandbox that ran ``model`` — sums for counts/tokens/cost, pooled
    latencies for honest p95, event-span wall clock. ``sandbox_count``
    reports how many cells the model covers.
    """

    model: str
    # Price-table key this model's ``estimated_cost_usd`` was computed at
    # (its own rate in auto mode, the uniform ``?price_model=`` otherwise).
    price_model: str = ""
    sandbox_count: int = 0
    llm_turns: int = 0
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: LatencyStats = Field(default_factory=LatencyStats)
    tool_calls: ToolCallStats = Field(default_factory=ToolCallStats)
    skill_calls: SkillCallStats = Field(default_factory=SkillCallStats)
    shell_commands: int = 0
    file_ops: int = 0
    test_executions: int = 0
    errors: int = 0
    wall_clock_seconds: float = 0.0
    estimated_cost_usd: float = 0.0
    evaluator: EvaluatorActivity | None = None


class RunAnalytics(BaseModel):
    """GET /api/runs/{run_id}/analytics response."""

    run_id: uuid.UUID
    model_name: str
    # The explicit ``?price_model=`` repricing basis, or None for auto mode —
    # each sandbox (and model rollup) costed at its OWN model's listed rate,
    # falling back to the run's resolved rate for unlisted models. Mirrors
    # ``RunTrends.price_model``.
    price_model: str | None = None
    price_table: dict[str, ModelPrice] = Field(default_factory=dict)
    sandboxes: list[SandboxAnalytics] = Field(default_factory=list)
    # PR14: participant-model rollups (one entry per model in the run's
    # matrix; a single entry for single-model runs).
    models: list[ModelAnalytics] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# PR6 — objective scoring signals (scoring/ package)
# ---------------------------------------------------------------------------

# ``AnalysisReport`` (imported at top) is the score payload for objective
# signals: the evaluator writes ``<artifact_dir>/analysis.json`` in this
# exact shape, the results API re-validates it and embeds it on
# ``SandboxResult.analysis``. ``scoring.models`` is pydantic-only (no heavy
# deps) so importing it from this module is cheap for every service that
# loads api.schemas.


# ---------------------------------------------------------------------------
# F13 — cross-run trends (api/routers/analytics.py GET /api/analytics/runs)
# ---------------------------------------------------------------------------


class RunTrendPoint(BaseModel):
    """One run's aggregate for the cross-run trend chart.

    Token/cost numbers keep the participant-vs-evaluator split (same
    ``eval_started`` boundary rule as the per-run endpoint). Cost defaults
    to each cell's own model rate (PR14 per-model pricing) unless an
    explicit ``?price_model=`` reprices every point uniformly.
    """

    run_id: uuid.UUID
    status: str
    task_prompt: str  # truncated to 120 chars server-side
    model_name: str
    evaluator_model: str | None = None
    created_at: datetime | None = None
    finished_at: datetime | None = None
    sandbox_count: int = 0
    # Participant-phase totals across all of the run's sandboxes.
    llm_turns: int = 0
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    errors: int = 0
    # Judge-phase totals (evaluator creds share the participant sandbox_id).
    evaluator_llm_turns: int = 0
    evaluator_tokens: TokenUsage = Field(default_factory=TokenUsage)
    evaluator_errors: int = 0
    # Which price_table entry ``estimated_cost_usd`` was computed at —
    # the explicit param, or the run's resolved rate in auto mode. None
    # when auto mode priced cells at mixed per-model rates (no single key).
    price_model: str | None = None
    estimated_cost_usd: float = 0.0
    # Judge tokens priced at the evaluator model's rate when it's listed,
    # else the run's resolved rate (explicit param or resolved fallback).
    evaluator_cost_usd: float = 0.0
    # Mean of the run's ``overall`` score rows; None when unscored.
    mean_overall_score: float | None = None


class RunTrends(BaseModel):
    """GET /api/analytics/runs response — newest-first point list."""

    # The explicit ``?price_model=`` repricing basis, or None when each
    # point was costed at its cells' own model rates ("auto" mode).
    price_model: str | None = None
    price_table: dict[str, ModelPrice] = Field(default_factory=dict)
    points: list[RunTrendPoint] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# PR22 — task preset library (api/routers/presets.py)
# ---------------------------------------------------------------------------


class PresetOut(BaseModel):
    """One task preset — a vendored ``task_presets/<slug>.md`` file or a
    user-saved ``task_presets`` row. Both sources read identically; ``source``
    distinguishes them and decides PATCH/DELETE-ability (user only)."""

    slug: str
    title: str
    description: str = ""
    # The verbatim task text a preset submit runs — same contract as
    # RunCreate.task_prompt.
    task_prompt: str
    category: str = ""
    tags: list[str] = Field(default_factory=list)
    source: str = "user"  # "vendored" | "user"
    # Bumps on every user-preset PATCH (vendored presets are immutable →
    # always 1). runs.preset_revision snapshots this at submit so the run
    # records which preset version its prompt came from.
    revision: int = 1
    suggested_adapter: str | None = None
    suggested_models: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # Library-browse aggregates — every run whose preset_ref matches the
    # slug (any preset_revision — a run keeps pointing at the preset it was
    # submitted from), plus the newest *scored* run's mean ``overall``
    # score (null when nothing on the preset is scored yet).
    runs_count: int = 0
    last_run_mean: float | None = None


class PresetCreate(BaseModel):
    """POST /api/presets/{slug} body — save a task prompt as a user preset.

    The slug comes from the path (one namespace shared with vendored
    presets — a collision 409s with a ``suggested_name`` for the rename
    flow). ``revision`` starts at 1.
    """

    title: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        min_length=1, max_length=255
    )
    task_prompt: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        min_length=1, max_length=100_000
    )
    description: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        default="", max_length=2000
    )
    category: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        default="", max_length=64
    )
    tags: list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)]] = Field(
        default_factory=list, max_length=16
    )
    suggested_adapter: Annotated[str, StringConstraints(strip_whitespace=True)] | None = Field(
        default=None, max_length=64
    )
    suggested_models: list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]] | None = Field(
        default=None, max_length=8
    )

    @field_validator("tags", "suggested_models", mode="after")
    @classmethod
    def _drop_blank_items(cls, v: list[str] | None) -> list[str] | None:
        if isinstance(v, list):
            return [t for t in v if t]
        return v


class PresetUpdate(BaseModel):
    """PATCH /api/presets/{slug} body — partial edit of a USER preset.

    Every applied field replaces the stored value; omitted fields keep
    theirs. Any successful PATCH bumps ``revision`` — runs snapshot it, so
    pre-edit runs keep pointing at the revision they ran on.
    """

    title: Annotated[str, StringConstraints(strip_whitespace=True)] | None = Field(
        default=None, min_length=1, max_length=255
    )
    task_prompt: Annotated[str, StringConstraints(strip_whitespace=True)] | None = Field(
        default=None, min_length=1, max_length=100_000
    )
    description: Annotated[str, StringConstraints(strip_whitespace=True)] | None = Field(
        default=None, max_length=2000
    )
    category: Annotated[str, StringConstraints(strip_whitespace=True)] | None = Field(
        default=None, max_length=64
    )
    tags: list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)]] | None = Field(
        default=None, max_length=16
    )
    suggested_adapter: Annotated[str, StringConstraints(strip_whitespace=True)] | None = Field(
        default=None, max_length=64
    )
    suggested_models: list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]] | None = Field(
        default=None, max_length=8
    )

    @field_validator("tags", "suggested_models", mode="after")
    @classmethod
    def _drop_blank_items(cls, v: list[str] | None) -> list[str] | None:
        if isinstance(v, list):
            return [t for t in v if t]
        return v


class PresetRunPoint(BaseModel):
    """One run's row inside a preset's analytics — the preset detail page's
    "every run on this preset" table row."""

    run_id: uuid.UUID
    status: str
    created_at: datetime | None = None
    finished_at: datetime | None = None
    sandbox_count: int = 0
    # Mean of the run's sandbox-level ``overall`` scores (null unscored)
    # + how many cells the mean covers.
    mean_overall_score: float | None = None
    scored_sandboxes: int = 0


class PresetLeaderboardEntry(BaseModel):
    """One row of a preset leaderboard — mean ``overall`` score pooled over
    every scored sandbox cell in the bucket across all runs on the preset."""

    key: str  # group key — model name, or ``name:<name>`` / ``cfg:<adapter>|<packs>|<extra>``
    label: str  # display label — model name, or ``adapter · packs [· 'extra']`` / config name
    adapter: str | None = None  # config board only: the grouped adapter
    name: str | None = None  # config board only: PR1 config name when set
    mean_overall_score: float
    n: int  # scored sandbox cells in the bucket
    runs: int  # distinct runs contributing a scored cell


class PresetTrendPoint(BaseModel):
    """Score-over-time point for the preset detail chart — one per run that
    has at least one scored cell, oldest first."""

    run_id: uuid.UUID
    created_at: datetime | None = None
    finished_at: datetime | None = None
    mean_overall_score: float
    scored_sandboxes: int = 0


class PresetAnalytics(BaseModel):
    """GET /api/presets/{slug}/analytics — everything the preset detail page
    needs in one shot: the preset itself, every run on it, per-model and
    per-config leaderboards, the pooled mean, and the score-over-time trend.
    """

    preset: PresetOut
    runs: list[PresetRunPoint] = Field(default_factory=list)  # newest first
    by_model: list[PresetLeaderboardEntry] = Field(default_factory=list)
    by_config: list[PresetLeaderboardEntry] = Field(default_factory=list)
    # Pooled mean ``overall`` over every scored cell on the preset + that
    # count; null when nothing on the preset has been scored yet.
    mean_overall_score: float | None = None
    n_scored: int = 0
    trend: list[PresetTrendPoint] = Field(default_factory=list)  # oldest first


# ---------------------------------------------------------------------------
# PR26 — benchmark suites + deterministic checkers (api/routers/suites.py)
# ---------------------------------------------------------------------------


class SuiteCheckOut(BaseModel):
    """One declared deterministic check on a suite task — ``check`` is the
    checker type (``file_exists`` | ``grep`` | ``py_test`` |
    ``output_grep``), ``spec`` the checker's declared parameters."""

    check: str
    spec: dict[str, Any] = Field(default_factory=dict)


class SuiteTaskOut(BaseModel):
    """One entry of a suite.yaml task list — ``preset_ref`` XOR ``prompt``.

    ``adapter`` optionally restricts the task to harness configs selecting
    that adapter (e.g. ``miniloop`` container cells vs the PR25 ``prompt``
    cell); null = the task runs on every submitted config.
    """

    index: int  # 0-based position inside the suite (== runs.suite_index)
    id: str  # declared id, else the preset slug, else ``task-<index>``
    preset_ref: str | None = None
    prompt: str | None = None
    adapter: str | None = None
    checks: list[SuiteCheckOut] = Field(default_factory=list)


class SuiteRunRef(BaseModel):
    """A ``suite_runs`` batch row — one fan-out of a suite."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    suite_slug: str
    suite_version: int | None = None
    created_at: datetime
    run_count: int = 0  # child runs in the batch (filled by the router)


class SuiteOut(BaseModel):
    """One vendored benchmark suite (``benchmark_suites/<slug>/``)."""

    slug: str
    version: int
    title: str
    description: str = ""
    task_count: int
    checks_count: int = 0  # declared checkers across all tasks
    # Browse aggregate — fan-out batches launched against this suite.
    suite_runs_count: int = 0


class SuiteDetailOut(SuiteOut):
    """GET /api/suites/{slug} — the suite plus its task entries and the
    most recent fan-out batches."""

    tasks: list[SuiteTaskOut] = Field(default_factory=list)
    recent_runs: list[SuiteRunRef] = Field(default_factory=list)


class SuiteRunIn(BaseModel):
    """POST /api/suites/{slug}/run body — the shared run spec every suite
    task fans out with (same fields as ``RunCreate`` minus the task text,
    which each suite task supplies via preset_ref or an inline prompt)."""

    model_name: Annotated[str, StringConstraints(strip_whitespace=True)] = Field(
        min_length=1, max_length=255
    )
    models: (
        list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)]]
        | None
    ) = Field(default=None, max_length=8)
    harness_configs: list[HarnessConfigIn] = Field(min_length=1, max_length=8)
    evaluator_model: Annotated[str, StringConstraints(strip_whitespace=True)] | None = (
        Field(default=None, max_length=255)
    )
    budgets: BudgetsIn = Field(default_factory=BudgetsIn)

    @model_validator(mode="after")
    def _normalize_models(self) -> SuiteRunIn:
        """Same dedupe/blank-drop + matrix cap as ``RunCreate`` — each task
        expands configs × models the same way a normal submit would."""
        if self.models is not None:
            seen = {self.model_name}
            cleaned: list[str] = []
            for m in self.models:
                if m and m not in seen:
                    seen.add(m)
                    cleaned.append(m)
            self.models = cleaned or None
        model_count = 1 + len(self.models or [])
        pinned = sum(1 for hc in self.harness_configs if hc.model)
        cells = pinned + (len(self.harness_configs) - pinned) * model_count
        if cells > MAX_MATRIX_CELLS:
            raise ValueError(
                f"run matrix would spawn {cells} sandboxes "
                f"({len(self.harness_configs)} configs × "
                f"{model_count} models); "
                f"max is {MAX_MATRIX_CELLS}"
            )
        return self


class SuiteRunChildOut(BaseModel):
    """One run created by a suite fan-out."""

    run_id: uuid.UUID
    suite_index: int
    task_id: str
    preset_ref: str | None = None
    status: str = "submitted"


class SuiteRunOut(BaseModel):
    """201 response to POST /api/suites/{slug}/run — the batch row plus the
    fanned-out child runs (order = suite.yaml task order)."""

    id: uuid.UUID
    suite_slug: str
    suite_version: int | None = None
    runs: list[SuiteRunChildOut] = Field(default_factory=list)
    # Submit-time advisory warnings from the model-catalog check (same
    # contract as RunOut.warnings — computed once for the shared spec).
    warnings: list[str] = Field(default_factory=list)


class CheckerResultOut(BaseModel):
    """One deterministic checker outcome — pass/fail/skipped/error +
    detail. ``spec`` echoes the declared check so the UI can label it."""

    check: str
    spec: dict[str, Any] = Field(default_factory=dict)
    status: str  # pass | fail | skipped | error
    passed: bool = False
    detail: str = ""
    created_at: datetime | None = None


class SuiteCellChecksOut(BaseModel):
    """Checker roll-up for one suite cell — counts by status plus the
    per-check detail rows (newest-wins, like the score merge)."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    # passed / (passed + failed) — skipped/error checks aren't measured
    # outcomes; null when nothing ran to a verdict.
    pass_rate: float | None = None
    checks: list[CheckerResultOut] = Field(default_factory=list)


class SuiteCellOut(BaseModel):
    """One task × cell result inside a suite run — a sandbox's judge score
    beside its deterministic checker roll-up."""

    sandbox_id: uuid.UUID
    name: str | None = None
    # Column label — config name or ``<adapter>[ · packs][ · model]`` — the
    # suite view groups cells across tasks by this key.
    label: str
    model: str = ""
    status: str
    terminal_reason: str | None = None
    # Newest-wins judge ``overall`` score (null while unscored).
    judge_overall: float | None = None
    checks: SuiteCellChecksOut = Field(default_factory=SuiteCellChecksOut)


class SuiteRunTaskOut(BaseModel):
    """One suite task's run inside a batch — task provenance + its cells."""

    run_id: uuid.UUID
    suite_index: int
    task_id: str
    preset_ref: str | None = None
    status: str
    created_at: datetime | None = None
    finished_at: datetime | None = None
    cells: list[SuiteCellOut] = Field(default_factory=list)


class SuiteRunAggregate(BaseModel):
    """Batch-level roll-up — the "mean" beside per-case pass/fail.

    ``check_pass_rate`` pools every cell's measured checks (passed /
    measured); ``mean_judge_overall`` averages cell-level judge overalls
    (each cell counted once). Nulls = nothing measured yet.
    """

    tasks: int = 0
    cells: int = 0
    checks_total: int = 0
    checks_passed: int = 0
    checks_failed: int = 0
    checks_skipped: int = 0
    checks_errors: int = 0
    check_pass_rate: float | None = None
    cells_scored: int = 0
    mean_judge_overall: float | None = None


class SuiteRunResults(BaseModel):
    """GET /api/suite-runs/{id} — the task × cell aggregation for one
    fan-out batch: per task, per sandbox cell, the judge ``overall`` score
    and the deterministic checker pass rate."""

    id: uuid.UUID
    suite_slug: str
    suite_version: int | None = None
    created_at: datetime | None = None
    runs: list[SuiteRunTaskOut] = Field(default_factory=list)
    aggregate: SuiteRunAggregate = Field(default_factory=SuiteRunAggregate)
