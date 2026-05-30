"""add_missing_enum_values

Add missing enum values PARTIALLY_REFUNDED and SPLIT to order_status.
Note: postgres ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
We commit the current transaction first, run ALTER TYPE, then start a new transaction.

Revision ID: 094c05ba33c3
Revises: e95a4a558afb
Create Date: 2026-07-02 16:33:15.105179
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '094c05ba33c3'
down_revision: Union[str, Sequence[str], None] = 'e95a4a558afb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# enum values to add
_MISSING_ENUM_VALUES = [
    "PARTIALLY_REFUNDED",
    "SPLIT",
]


def upgrade() -> None:
    bind = op.get_bind()

    for value in _MISSING_ENUM_VALUES:
        # postgres requires alter type to run outside a transaction block
        bind.execute(sa.text("COMMIT"))
        bind.execute(
            sa.text(
                f"ALTER TYPE order_status ADD VALUE IF NOT EXISTS '{value}'"
            )
        )
        bind.execute(sa.text("BEGIN"))


def downgrade() -> None:
    # postgres does not support dropping enum values easily
    pass
