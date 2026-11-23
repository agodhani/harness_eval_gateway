"""model_prices table — user-editable glob price table

Revision ID: c4e8d1a7f3b5
Revises: d7e9f1a3b5c7
Create Date: 2026-04-30

PR28 — persistent price table layered over ``api.pricing.PRICE_TABLE``:
``model_glob`` is an fnmatch pattern matched case-insensitively in
``created_at, id`` order (first match wins); a hit overrides the static
rate for that model. ``cache_read_per_mtok_usd`` is NULL when the provider
publishes no discounted cache-read rate — cached input tokens then bill at
the input rate exactly as before. ``source`` distinguishes ``user`` rows
(PUT /api/prices, never overwritten by sync) from ``openrouter`` rows
(catalog sync, refreshed on each run).

Merge note: PR27's ``d7e9f1a3b5c7_runs_trials`` landed first off the
same ``a2b3c4d5e6f7`` parent — this revision re-points
``down_revision`` at it to keep a single migration head.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c4e8d1a7f3b5"
down_revision = "d7e9f1a3b5c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_prices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_glob", sa.String(length=255), nullable=False),
        sa.Column("input_per_mtok_usd", sa.Float(), nullable=False),
        sa.Column("output_per_mtok_usd", sa.Float(), nullable=False),
        sa.Column("cache_read_per_mtok_usd", sa.Float(), nullable=True),
        sa.Column(
            "local", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "source", sa.String(length=32), nullable=False, server_default="user"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_glob", name="uq_model_prices_model_glob"),
    )


def downgrade() -> None:
    op.drop_table("model_prices")
