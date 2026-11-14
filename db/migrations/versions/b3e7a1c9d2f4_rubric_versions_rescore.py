"""rubric versions + score provenance

Revision ID: b3e7a1c9d2f4
Revises: f0a1b2c3d4e5
Create Date: 2025-11-24

PR5 — post-run rubric edits land as new ``rubrics`` rows: ``parent_id``
points at the version the edit was based on (v1 stays NULL). Scores keep
``rubric_version`` provenance — a denormalized copy of ``rubrics.version``
at insert time — so re-score rows stay distinguishable from the original
judging without a join. Existing rows are backfilled via ``rubric_id``.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "b3e7a1c9d2f4"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table: sqlite can't ADD CONSTRAINT; a no-op wrapper on
    # Postgres so both dialects take the same path.
    with op.batch_alter_table("rubrics") as batch:
        batch.add_column(sa.Column("parent_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_rubrics_parent_id", "rubrics", ["parent_id"], ["id"]
        )
        batch.create_index("ix_rubrics_parent_id", ["parent_id"])

    with op.batch_alter_table("scores") as batch:
        batch.add_column(sa.Column("rubric_version", sa.Integer(), nullable=True))

    # Backfill provenance for pre-versioning score rows through rubric_id.
    op.execute(
        "UPDATE scores SET rubric_version = "
        "(SELECT version FROM rubrics WHERE rubrics.id = scores.rubric_id) "
        "WHERE rubric_id IS NOT NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("scores") as batch:
        batch.drop_column("rubric_version")
    with op.batch_alter_table("rubrics") as batch:
        batch.drop_index("ix_rubrics_parent_id")
        batch.drop_constraint("fk_rubrics_parent_id", type_="foreignkey")
        batch.drop_column("parent_id")
