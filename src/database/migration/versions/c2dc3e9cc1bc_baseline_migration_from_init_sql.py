"""Baseline migration from init.sql

Deliberately empty. A fresh database is created by executing ``init.sql``
(see ``migration_manager.initialize_from_sql``); this revision exists only to
give that state a name so subsequent migrations have something to chain from.

Revision ID: c2dc3e9cc1bc
Revises:
Create Date: 2026-09-06

"""

# revision identifiers, used by Alembic.
revision = "c2dc3e9cc1bc"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Upgrade database schema."""
    pass


def downgrade() -> None:
    """Downgrade database schema."""
    pass
