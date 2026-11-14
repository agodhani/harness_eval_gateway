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
    # submitted | rubric_review | provisioning | running | evaluating | done | failed
    status: Mapped[str] = mapped_column(String(32), default="submitted", index=True)
    task_prompt: Mapped[str] = mapped_column(Text)
    model_name: Mapped[str] = mapped_column(String(255))
    # Harness configs, skill packs, evaluator model — shape refined in F1/F2.
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sandboxes: Mapped[list[Sandbox]] = relationship(back_populates="run")
    rubrics: Mapped[list[Rubric]] = relationship(back_populates="run")


class Sandbox(Base):
    """One Docker container running a single harness instance on a run."""

    __tablename__ = "sandboxes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("runs.id"), index=True)
    # Per-sandbox harness config (which adapter, skill packs, prompt overrides).
    harness_config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
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
    # llm_request | llm_response | tool_call | shell_command | file_op |
    # test_execution | checkpoint | skill_invocation | done_signal | error |
    # run_started | terminal_state | context_compacted | miniloop_result
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
    dimension: Mapped[str] = mapped_column(String(128))
    score: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sandbox: Mapped[Sandbox] = relationship(back_populates="scores")
    rubric: Mapped[Rubric | None] = relationship(back_populates="scores")


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
