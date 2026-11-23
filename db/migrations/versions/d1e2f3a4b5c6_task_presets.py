"""task presets + run preset_ref

Revision ID: d1e2f3a4b5c6
Revises: d6e2b8f4a1c9
Create Date: 2026-02-12

PR22 — task preset library. ``task_presets`` stores USER-saved presets
(vendored presets stay files under ``task_presets/`` and never land here);
``slug`` is unique because presets share one vendored+user slug namespace.
``runs.preset_ref`` + ``runs.preset_revision`` snapshot which preset (and
which revision of it) a submitted run's task_prompt came from — provenance
for preset-scoped grouping, leaderboards, and score-over-time views.

Merge note: originally branched off ``a2b3c4d5e6f7``; PR27 (``d7e9f1a3b5c7``
runs_trials) + PR28 (``c4e8d1a7f3b5`` model_prices, then its
``d6e2b8f4a1c9`` model_prices_lower_unique follow-up) landed on main first,
so ``down_revision`` re-points at the newer head ``d6e2b8f4a1c9`` to keep a
single migration head.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "d1e2f3a4b5c6"
down_revision = "d6e2b8f4a1c9"
branch_labels = None
depends_on = None

# JSONB on Postgres, plain JSON on other dialects (mirrors db/models.py).
JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "task_presets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("task_prompt", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("tags", JSONB, nullable=False),
        # Rows are always "user" — "vendored" is reserved for file presets.
        sa.Column("source", sa.String(length=16), nullable=False, server_default="user"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("suggested_adapter", sa.String(length=64), nullable=True),
        sa.Column("suggested_models", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("slug", name="uq_task_presets_slug"),
    )

    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("preset_ref", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("preset_revision", sa.Integer(), nullable=True))
        batch.create_index("ix_runs_preset_ref", ["preset_ref"])


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.drop_index("ix_runs_preset_ref")
        batch.drop_column("preset_revision")
        batch.drop_column("preset_ref")
    op.drop_table("task_presets")
