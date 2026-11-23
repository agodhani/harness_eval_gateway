"""SQLAlchemy 2.x declarative models — the harness_eval_gateway schema.

Status fields are plain strings for now (validated at the API boundary later);
allowed values are noted per column. Timestamps are timestamptz.

`events` is APPEND-ONLY by convention (AGENTS.md): rows are inserted, never
updated or deleted — corrections are new events.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSONB on Postgres, plain JSON elsewhere (sqlite tests).
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class Run(Base):
    """One submitted evaluation: task prompt + model + harness configs."""

    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # submitted | rubric_review | queued (PR15 pacing: parked behind the
    # active run) | provisioning | running | evaluating |
    # done | failed | cancelled (user-cancelled pre-spawn; DELETE /api/runs)
    status: Mapped[str] = mapped_column(String(32), default="submitted", index=True)
    task_prompt: Mapped[str] = mapped_column(Text)
    model_name: Mapped[str] = mapped_column(String(255))
    # PR27 — repeats per matrix cell (1–8): every effective config × model
    # cell spawns ``trials`` sandboxes, each stamped
    # ``harness_config.trial_index`` (1-based). Default 1 = pre-PR27 shape.
    trials: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    # Harness configs, skill packs, evaluator model — shape refined in F1/F2.
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # PR22 preset provenance — the preset slug the task_prompt was taken from
    # (vendored file or user row) and the revision it had at submit time.
    # task_prompt stays the verbatim contract; these are grouping/provenance.
    preset_ref: Mapped[str | None] = mapped_column(String(64), index=True)
    preset_revision: Mapped[int | None] = mapped_column(Integer)
    # PR26 suite provenance — set when the run was fanned out by
    # POST /api/suites/{slug}/run: the suite slug, the 0-based index of this
    # run's task inside suite.yaml, and the shared suite_runs batch row that
    # groups the fan-out for aggregation.
    suite_ref: Mapped[str | None] = mapped_column(String(64), index=True)
    suite_index: Mapped[int | None] = mapped_column(Integer)
    suite_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("suite_runs.id"), index=True
    )

    sandboxes: Mapped[list[Sandbox]] = relationship(back_populates="run")
    rubrics: Mapped[list[Rubric]] = relationship(back_populates="run")
    suite_run: Mapped[SuiteRun | None] = relationship(back_populates="runs")


class Sandbox(Base):
    """One Docker container running a single harness instance on a run."""

    __tablename__ = "sandboxes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.id"), index=True)
    # Per-sandbox harness config (which adapter, skill packs, prompt overrides).
    harness_config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    # Optional user label for the harness config (PR1 named configs) —
    # surfaced in timeline/results/analytics; provisioner defaults it to
    # "<adapter>-<sandbox short id>" when the submit left it blank.
    name: Mapped[str | None] = mapped_column(String(255))
    # provisioning | running | terminating | done | failed (terminal_reason set)
    status: Mapped[str] = mapped_column(String(32), default="provisioning", index=True)
    docker_container_id: Mapped[str | None] = mapped_column(String(128))
    artifact_path: Mapped[str | None] = mapped_column(String(1024))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # agent_done | harness_complete | timeout | budget_exceeded | stuck_loop | error
    terminal_reason: Mapped[str | None] = mapped_column(String(32))

    run: Mapped[Run] = relationship(back_populates="sandboxes")
    events: Mapped[list[Event]] = relationship(back_populates="sandbox")
    scores: Mapped[list[Score]] = relationship(back_populates="sandbox")
    artifacts: Mapped[list[Artifact]] = relationship(back_populates="sandbox")
    checker_results: Mapped[list[CheckerResult]] = relationship(
        back_populates="sandbox"
    )


class Event(Base):
    """Append-only structured event — powers the live + persisted timeline.

    APPEND-ONLY: never UPDATE or DELETE rows; corrections are new events.
    """

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_run_sandbox_ts", "run_id", "sandbox_id", "ts"),)

    # BIGINT on Postgres; INTEGER on sqlite so it becomes an INTEGER PRIMARY
    # KEY (rowid alias) and autoincrement actually works in tests — sqlite
    # does not auto-assign BIGINT PKs.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.id"), index=True)
    # Nullable: run-level events (rubric gen, orchestration) have no sandbox.
    sandbox_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("sandboxes.id"), index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Ordering hint within a source stream; nullable until ingestion defines it.
    seq: Mapped[int | None] = mapped_column(BigInteger)
    # gateway | harness | sandbox | evaluator | orchestrator
    source: Mapped[str] = mapped_column(String(32))
    # llm_request | llm_response | tool_call | tool_result | shell_command |
    # file_op | test_execution | checkpoint | skill_invocation | done_signal |
    # error | run_started | terminal_state | context_compacted | miniloop_result
    # (authoritative list: api/schemas.py EventType — keep in sync)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    sandbox: Mapped[Sandbox | None] = relationship(back_populates="events")


class Rubric(Base):
    """Scoring spec for a run; frozen (status=approved) before launch.

    Post-run edits append NEW rows (PR5): ``version`` increments,
    ``parent_id`` points at the version the edit was based on, and the
    previous approved row is marked ``superseded`` — the old row stays so
    scores written against it keep their provenance.
    """

    __tablename__ = "rubrics"
    __table_args__ = (
        # One row per (run, version) — concurrent version-creating POSTs
        # must collide on this, not silently duplicate a version number.
        UniqueConstraint("run_id", "version", name="uq_rubrics_run_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Self-FK: the rubric version this one was edited from (null for v1).
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("rubrics.id"), index=True
    )
    content: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    # draft | approved | superseded
    status: Mapped[str] = mapped_column(String(16), default="draft")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped[Run] = relationship(back_populates="rubrics")
    scores: Mapped[list[Score]] = relationship(back_populates="rubric")


class Score(Base):
    """Evaluator score for one rubric dimension of one sandbox artifact."""

    __tablename__ = "scores"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sandbox_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sandboxes.id"), index=True)
    rubric_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("rubrics.id"), index=True)
    # Denormalized copy of rubrics.version at insert time (PR5) — keeps old
    # vs new re-score rows distinguishable without a join.
    rubric_version: Mapped[int | None] = mapped_column(Integer)
    # Durable insertion order (PR5) — the evaluator stamps max(seq)+1 per
    # pass so the newest-wins merge can break created_at ties (sqlite
    # stores seconds) without guessing from rubric_version. NULL = legacy
    # or unstamped rows; sorted first.
    seq: Mapped[int | None] = mapped_column(BigInteger)
    dimension: Mapped[str] = mapped_column(String(128))
    score: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sandbox: Mapped[Sandbox] = relationship(back_populates="scores")
    rubric: Mapped[Rubric | None] = relationship(back_populates="scores")


class TaskPreset(Base):
    """PR22 — user-saved task preset (the "user" side of the preset library).

    Vendored presets are files under ``task_presets/`` and never appear here;
    rows in this table are always ``source="user"`` and share the vendored
    slug namespace (creation rejects vendored-name collisions). ``revision``
    bumps on every PATCH and is what ``runs.preset_revision`` snapshots.
    """

    __tablename__ = "task_presets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # One slug namespace with the vendored files — URL token for
    # /api/presets/{slug} and runs.preset_ref.
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    task_prompt: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(64), default="")
    tags: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    # "user" for every row — "vendored" is reserved for file presets and
    # never written here (kept in the column doc for the shared namespace).
    source: Mapped[str] = mapped_column(String(16), default="user")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    # Optional submit-form hints — same names as the vendored frontmatter
    # keys so vendored and user presets read uniformly in PresetOut.
    suggested_adapter: Mapped[str | None] = mapped_column(String(64))
    suggested_models: Mapped[list[Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SuiteRun(Base):
    """PR26 — one fan-out batch from ``POST /api/suites/{slug}/run``.

    Each suite task becomes an independent Run (own rubric review,
    sandboxes, eval pass); this row is the grouping handle — ``runs.
    suite_run_id`` points back here so a suite view can aggregate the
    task × cell matrix (checker pass rates + judge scores) per batch.
    """

    __tablename__ = "suite_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Suite slug + the suite.yaml ``version`` at submit time — provenance
    # for which suite revision produced the fan-out.
    suite_slug: Mapped[str] = mapped_column(String(64), index=True)
    suite_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    runs: Mapped[list[Run]] = relationship(back_populates="suite_run")


class CheckerResult(Base):
    """PR26 — one deterministic checker outcome on a sandbox artifact.

    Suite task checks (file_exists/grep/py_test/output_grep) are measured
    post-artifact by the orchestrator before each eval pass; results live
    in their own table — sibling of scores, NOT Score rows — so the
    objective verdict complements the judge without joining rubric
    provenance. Re-runs (rescore) append new rows; readers merge
    newest-wins per (sandbox, check, spec).
    """

    __tablename__ = "checker_results"

    # Autoincrement pk like events.id — the durable insertion order that
    # newest-wins merging keys on (created_at is only second-precision on
    # sqlite).
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.id"), index=True)
    sandbox_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sandboxes.id"), index=True
    )
    # Checker type — file_exists | grep | py_test | output_grep |
    # exact | contains | regex | json_schema | numeric_tol.
    check_type: Mapped[str] = mapped_column(String(32))
    # The declared check spec echoed back ({path}, {path,pattern}, ...).
    spec: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    # pass | fail | skipped (exec env absent — can't measure) | error
    # (checker/spec failure — a red check is a measurement, not an error).
    status: Mapped[str] = mapped_column(String(16))
    # True only for status="pass"; False for fail/skipped/error — kept as a
    # convenience column so pass-rate SQL stays trivial.
    passed: Mapped[bool] = mapped_column(default=False)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    sandbox: Mapped[Sandbox] = relationship(back_populates="checker_results")


class ModelPriceRow(Base):
    """PR28 — user-editable model price rate, layered over the static table.

    ``model_glob`` is an fnmatch pattern (``gpt-4*``) matched
    case-insensitively; rows are tried in declaration order (``created_at``,
    ``id``) and the FIRST match wins, then the static ``PRICE_TABLE`` falls
    back (api/pricing.py::PriceBook). ``cache_read_per_mtok_usd`` is nullable:
    NULL bills cached input tokens at the input rate like before, a value
    moves them to the provider's discounted cache-read rate. ``source`` is
    ``user`` (PUT /api/prices) or ``openrouter`` (catalog sync) — sync never
    clobbers a user row.
    """

    __tablename__ = "model_prices"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    model_glob: Mapped[str] = mapped_column(String(255), unique=True)
    input_per_mtok_usd: Mapped[float] = mapped_column(Float, default=0.0)
    output_per_mtok_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cache_read_per_mtok_usd: Mapped[float | None] = mapped_column(Float)
    local: Mapped[bool] = mapped_column(Boolean, default=False)
    # user | openrouter
    source: Mapped[str] = mapped_column(String(32), default="user")
    # Declaration order for PriceBook's first-match-wins scan.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Artifact(Base):
    """Metadata for files in the artifact store (./data/artifacts/)."""

    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sandbox_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sandboxes.id"), index=True)
    # repo_tarball | transcript | event_export | screenshot | ...
    kind: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(String(1024))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sandbox: Mapped[Sandbox] = relationship(back_populates="artifacts")
