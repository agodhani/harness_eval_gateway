"""rubric templates + preset suggested_rubric

Revision ID: f6a7b8c9d0e1
Revises: c5f9a2b8d4e7
Create Date: 2026-02-16

PR23 — custom rubric dimensions + rubric templates.

``rubric_templates`` is the user side of the rubric-template library —
mirroring ``task_presets``: vendored ``rubric_templates/<name>.json`` files
(code-default, extraction-accuracy, summarisation-fidelity,
support-tone-compliance) ship the committed defaults and never appear in
this table; rows are ``source="user"`` and share the vendored name
namespace. ``content`` is the RubricContent-shaped dict used as the
generation basis when ``runs.config.rubric_template`` selects the name.

``task_presets.suggested_rubric`` (nullable JSONB) lets a preset declare
its rubric basis — ``{"template": <name>}``, ``{"hint": <text>}``, or an
inline ``{"dimensions": [...]}`` fragment. Submit applies it only when no
explicit ``rubric_template`` was submitted.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "f6a7b8c9d0e1"
down_revision = "c5f9a2b8d4e7"
branch_labels = None
depends_on = None

# JSONB on Postgres, plain JSON on other dialects (mirrors db/models.py).
JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "rubric_templates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "description", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column("content", JSONB, nullable=False),
        sa.Column(
            "source", sa.String(length=16), nullable=False, server_default="user"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_rubric_templates_name", "rubric_templates", ["name"], unique=True
    )

    with op.batch_alter_table("task_presets") as batch:
        batch.add_column(sa.Column("suggested_rubric", JSONB, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("task_presets") as batch:
        batch.drop_column("suggested_rubric")
    op.drop_index("ix_rubric_templates_name", table_name="rubric_templates")
    op.drop_table("rubric_templates")
