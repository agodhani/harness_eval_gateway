"""judge panel + score provenance + pairwise votes

Revision ID: c7a9e1b3d5f2
Revises: f6a7b8c9d0e1
Create Date: 2026-02-20

PR29 — judge quality (panel / human override / blind A-B).

``scores`` gains three provenance columns:

- ``judge_model`` — the evaluator model that authored the row, stamped at
  insert time so a multi-judge panel keeps per-judge rows attributable
  (NULL on human/checker rows and on every pre-panel ``llm`` row — the
  results merge treats a NULL judge as the run's primary
  ``config.evaluator_model``, the only model that could have written it).
- ``source`` — ``llm`` (judge pass), ``human`` (``POST
  /api/sandboxes/{id}/scores`` override — wins the newest-wins merge for
  its (sandbox, dimension) pair regardless of insertion order), or
  ``checker`` (deterministic ``suite_pass`` rows — no model authored
  them). Existing rows backfill to the ``llm`` default.
- ``note`` — free-text context on human overrides (why the score moved).

``pairwise_votes`` records blind A/B verdicts. A pair session assigns two
artifact-bearing sandboxes the anonymized labels A/B in randomized order;
each vote stores the REAL sandbox ids (``left_sandbox`` := the sandbox
behind label A) plus the picked winner (``left`` | ``right`` | ``tie``)
per dimension — anonymization is presentation-only, the vote rows feed
the results-page win-rate + judge-vs-human agreement stats.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "c7a9e1b3d5f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("scores") as batch:
        batch.add_column(
            sa.Column("judge_model", sa.String(length=255), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "source",
                sa.String(length=16),
                nullable=False,
                server_default="llm",
            )
        )
        batch.add_column(sa.Column("note", sa.Text(), nullable=True))

    op.create_table(
        "pairwise_votes",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("left_sandbox", sa.Uuid(), nullable=False),
        sa.Column("right_sandbox", sa.Uuid(), nullable=False),
        sa.Column("dimension", sa.String(length=128), nullable=False),
        sa.Column("winner", sa.String(length=8), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
        sa.ForeignKeyConstraint(["left_sandbox"], ["sandboxes.id"]),
        sa.ForeignKeyConstraint(["right_sandbox"], ["sandboxes.id"]),
    )
    op.create_index("ix_pairwise_votes_run_id", "pairwise_votes", ["run_id"])
    op.create_index(
        "ix_pairwise_votes_left_sandbox", "pairwise_votes", ["left_sandbox"]
    )
    op.create_index(
        "ix_pairwise_votes_right_sandbox", "pairwise_votes", ["right_sandbox"]
    )


def downgrade() -> None:
    op.drop_index("ix_pairwise_votes_right_sandbox", table_name="pairwise_votes")
    op.drop_index("ix_pairwise_votes_left_sandbox", table_name="pairwise_votes")
    op.drop_index("ix_pairwise_votes_run_id", table_name="pairwise_votes")
    op.drop_table("pairwise_votes")
    with op.batch_alter_table("scores") as batch:
        batch.drop_column("note")
        batch.drop_column("source")
        batch.drop_column("judge_model")
