"""runs.share_token column

Revision ID: c5f9a2b8d4e7
Revises: e4f5a6b7c8d9
Create Date: 2026-03-01

PR35 export + share — the stretch half of the card: ``runs.share_token``
is an unguessable, nullable, unique token minted by
``POST /api/runs/{id}/share`` (revoked by DELETE). It gates the read-only
``/share/{token}`` view + ``GET /api/share/{token}`` bundle (score matrix +
events-lite — the same projection the JSON export ships). NULL on every
pre-existing run = "not shared".

Uniqueness is enforced via a plain UNIQUE INDEX rather than a column
constraint so the migration stays portable to sqlite's narrow
``ALTER TABLE ADD COLUMN`` (fresh ``create_all`` gets the equivalent
column-level ``unique=True`` from the model).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c5f9a2b8d4e7"
down_revision = "e4f5a6b7c8d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("share_token", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_runs_share_token",
        "runs",
        ["share_token"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_runs_share_token", table_name="runs")
    op.drop_column("runs", "share_token")
