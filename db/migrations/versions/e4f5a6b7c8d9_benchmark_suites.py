"""benchmark suites + checker results

Revision ID: e4f5a6b7c8d9
Revises: d1e2f3a4b5c6
Create Date: 2026-02-13

PR26 — benchmark suites + deterministic checkers.

``suite_runs`` is the fan-out grouping row: ``POST /api/suites/{slug}/run``
creates one batch row plus one ``runs`` row per suite task. Each child run
carries ``suite_ref`` (the vendored suite slug), ``suite_index`` (its
0-based position inside suite.yaml's task list) and ``suite_run_id`` (the
batch) — provenance for suite-view aggregation and the ``?suite=`` runs
filter.

``checker_results`` records deterministic post-artifact check outcomes
(file_exists / grep / py_test / output_grep) per sandbox — objective
pass/fail evidence that complements LLM-judge scores without being folded
into them. ``id`` is an autoincrement BIGINT (INTEGER on sqlite) so the
durable insertion order survives re-score passes (newest-wins per
sandbox + check + spec); ``passed`` stays a plain boolean mirroring
``status == 'pass'`` so pass-rate aggregation is a trivial count.

Merge note: parallel PRs may add migrations off the same parent
(``d1e2f3a4b5c6``). Whichever lands second must re-point ``down_revision``
at the other's head to keep a single migration head.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "e4f5a6b7c8d9"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None

# JSONB on Postgres, plain JSON on other dialects (mirrors db/models.py).
JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "suite_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("suite_slug", sa.String(length=64), nullable=False),
        sa.Column("suite_version", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_suite_runs_suite_slug", "suite_runs", ["suite_slug"])

    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("suite_ref", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("suite_index", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("suite_run_id", sa.Uuid(), nullable=True))
        batch.create_index("ix_runs_suite_ref", ["suite_ref"])
        batch.create_index("ix_runs_suite_run_id", ["suite_run_id"])
        batch.create_foreign_key(
            "fk_runs_suite_run_id", "suite_runs", ["suite_run_id"], ["id"]
        )

    op.create_table(
        "checker_results",
        # BIGINT on Postgres, INTEGER on sqlite — the rowid-alias trick that
        # makes autoincrement work there (same as events.id).
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("sandbox_id", sa.Uuid(), nullable=False),
        sa.Column("check_type", sa.String(length=32), nullable=False),
        sa.Column("spec", JSONB, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name="fk_checker_results_run_id"
        ),
        sa.ForeignKeyConstraint(
            ["sandbox_id"], ["sandboxes.id"], name="fk_checker_results_sandbox_id"
        ),
    )
    op.create_index("ix_checker_results_run_id", "checker_results", ["run_id"])
    op.create_index(
        "ix_checker_results_sandbox_id", "checker_results", ["sandbox_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_checker_results_sandbox_id", table_name="checker_results")
    op.drop_index("ix_checker_results_run_id", table_name="checker_results")
    op.drop_table("checker_results")
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("fk_runs_suite_run_id", type_="foreignkey")
        batch.drop_index("ix_runs_suite_run_id")
        batch.drop_index("ix_runs_suite_ref")
        batch.drop_column("suite_run_id")
        batch.drop_column("suite_index")
        batch.drop_column("suite_ref")
    op.drop_index("ix_suite_runs_suite_slug", table_name="suite_runs")
    op.drop_table("suite_runs")
