"""Add migrations script template."""

from alembic import op
import sqlalchemy as sa

revision = "${revision}"
down_revision = ${down_revision}
branch_labels = ${branch_labels}
depends_on = ${depends_on}


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
