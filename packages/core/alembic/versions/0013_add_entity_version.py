"""Add version to entities and edges.

Revision ID: 0013_add_entity_version
Revises: 0012_add_outbox
Create Date: 2026-06-22 16:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013_add_entity_version"
down_revision: Union[str, None] = "0012_add_outbox"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("entities", sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"))
    op.add_column("edges", sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"))


def downgrade() -> None:
    op.drop_column("edges", "version")
    op.drop_column("entities", "version")
