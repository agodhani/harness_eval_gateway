"""Pydantic request/response schemas for the control-plane API.

F6 adds the event-ingestion/query schemas (enums mirror db/models.py column
docs). Run-spec schemas (F1) and results schemas (F8) land with their routers.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from api.pricing import ModelPrice


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
    CLI harnesses (``codex``, ``claude_code``, ``pi``, ``deepseek``; see
    ``orchestrator/adapters.py`` and ``GET /api/harnesses``). ``skill_packs``
    selects which vendored packs under ``./skillpacks`` are staged into the
    sandbox, and ``system_prompt_extra`` appends run-specific instructions
    (native ``--append-system-prompt`` where the harness has one, otherwise
    inlined into the task prompt footer — another comparison axis).
    """

    adapter: str = "miniloop"
    skill_packs: list[str] = Field(default_factory=list, max_length=16)
    system_prompt_extra: str | None = Field(default=None, max_length=8000)


class BudgetsIn(BaseModel):
    """Termination-policy knobs for a run.

    ``token_budget`` is PER-RUN (the gateway meters tokens across all of a
    run's sandboxes against one ceiling), not per-sandbox.
    """

    max_iters: int = Field(default=50, ge=1, le=1000)
    token_budget: int = Field(default=200_000, ge=1_000)
    wall_clock_seconds: int = Field(default=3600, ge=60, le=86_400)


class RunCreate(BaseModel):
    """POST /api/runs body — one evaluation submission."""

    task_prompt: str = Field(min_length=1, max_length=100_000)
    model_name: str = Field(min_length=1, max_length=255)
    harness_configs: list[HarnessConfigIn] = Field(min_length=1, max_length=8)
    # Defaults to model_name at submit (same-model eval is flagged, not banned).
    evaluator_model: str | None = Field(default=None, max_length=255)
    budgets: BudgetsIn = Field(default_factory=BudgetsIn)


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


class RubricOut(BaseModel):
    """A rubric row (draft or frozen/approved)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    version: int
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
    # True when the evaluator model equals the participant model — the UI
    # flags this so users notice non-independent judging.
    same_model_eval: bool
    created_at: datetime
    updated_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    sandboxes: list[SandboxOut] = Field(default_factory=list)
    rubric: RubricOut | None = None


class RunSummary(BaseModel):
    """One row of GET /api/runs — list view, no nested detail."""

    id: uuid.UUID
    status: str
    task_prompt: str  # truncated to 200 chars
    model_name: str
    created_at: datetime
    finished_at: datetime | None
    sandbox_count: int


# ---------------------------------------------------------------------------
# F10 — skillpack import (api/routers/skillpacks.py)
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

    ``pack_name`` is the target pack: a fresh name creates a new imported
    pack; an existing *imported* pack gets the skills appended (the
    "individual skills into an existing pack" flow). Vendored pack names are
    rejected so imports can never shadow vendored packs.
    """

    url: str = Field(min_length=1, max_length=2000)
    # Pin the discovered tree: resolved ref or commit_sha from the discover
    # response. Pair with ``subpath`` when pinning a sha for a branch whose
    # name contains slashes (its segment count differs from the URL's ref).
    ref: str | None = Field(default=None, max_length=255)
    subpath: str | None = Field(default=None, max_length=1000)
    # Repo-relative skill dirs (``dir`` values from /discover) to import.
    skills: list[str] = Field(min_length=1, max_length=64)
    pack_name: str = Field(min_length=1, max_length=64)


class ImportedSkill(BaseModel):
    """One skill actually written to disk."""

    source_dir: str  # repo-relative dir it came from
    name: str  # frontmatter name (dir fallback)
    dir: str  # final dir under <pack>/skills/
    file_count: int
    renamed: bool  # dir name was deduplicated (-2, -3…)


class SkillpackImportOut(BaseModel):
    """POST /api/skillpacks/import response."""

    pack: str
    source: str = "imported"
    skill_count: int  # total skills in the pack post-write (merges append)
    imported: list[ImportedSkill] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


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
    estimated_cost_usd: float = 0.0


class SandboxAnalytics(BaseModel):
    """Per-sandbox metrics for the analytics comparison table."""

    sandbox_id: uuid.UUID
    # Human label mirroring the results-matrix header (adapter · packs).
    label: str = ""
    harness_config: dict[str, Any] = Field(default_factory=dict)
    status: str
    terminal_reason: str | None = None
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
    # Which price_table entry ``estimated_cost_usd`` was computed at.
    price_model: str
    price_table: dict[str, ModelPrice] = Field(default_factory=dict)
    sandboxes: list[SandboxAnalytics] = Field(default_factory=list)
