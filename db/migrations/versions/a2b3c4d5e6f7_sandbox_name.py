"""sandbox name column

Revision ID: a2b3c4d5e6f7
Revises: b3e7a1c9d2f4
Create Date: 2026-02-10

PR1 named harness configs — optional per-config label persisted onto the
sandbox row so same-adapter sandboxes are distinguishable (timeline filter,
results matrix, analytics). NULL on pre-existing rows; the provisioner
assigns ``<adapter>-<sandbox short id>`` for new blank submissions, so the
column stays nullable.

Merge note: a sibling PR adds another migration off the same parent
(``f0a1b2c3d4e5``). Whichever lands second must re-point ``down_revision``
at the other's head to keep a single migration head — this migration
landed second and now points at ``b3e7a1c9d2f4`` (rubric versions).
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a2b3c4d5e6f7"
down_revision = "b3e7a1c9d2f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sandboxes", sa.Column("name", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("sandboxes", "name")
