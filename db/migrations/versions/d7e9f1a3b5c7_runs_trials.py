"""runs.trials column

Revision ID: d7e9f1a3b5c7
Revises: a2b3c4d5e6f7
Create Date: 2026-02-16

PR27 repeats-per-cell — each effective config × model matrix cell runs
``trials`` times (1–8, default 1) so rankings rest on more than one sample.
The trial index lives on the sandbox's ``harness_config`` JSON
(``trial_index``, 1-based — only stamped when trials > 1, matching the
``model`` injection pattern), so this migration only adds the run-level
spec column. Existing rows get ``trials=1`` via the server default.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d7e9f1a3b5c7"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "trials",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "trials")
