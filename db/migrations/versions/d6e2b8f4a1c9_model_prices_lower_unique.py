"""model_prices — case-insensitive glob uniqueness

Revision ID: d6e2b8f4a1c9
Revises: c4e8d1a7f3b5
Create Date: 2026-11-22

PR28 follow-up — ``model_glob`` identity is case-insensitive: PUT upserts,
``PriceBook`` matching, the picker, and sync's existing-row map all fold
case. The ``uq_model_prices_model_glob`` constraint is case-sensitive, so
a concurrent or differently-cased insert could commit a second row that
first-match-wins ordering then shadows permanently. A unique index on
``lower(model_glob)`` makes the database enforce the identity the writers
already assume — a lost select-then-insert race fails with IntegrityError
instead of storing a duplicate.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d6e2b8f4a1c9"
down_revision = "c4e8d1a7f3b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_model_prices_model_glob_lower",
        "model_prices",
        [sa.text("lower(model_glob)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_model_prices_model_glob_lower", table_name="model_prices"
    )
