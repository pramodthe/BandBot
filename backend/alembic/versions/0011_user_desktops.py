"""Separate user-owned VM hosts, agent displays, and request authority."""
from alembic import op
from app.persistence.models import UserDesktop, TaskPrincipal, ComputerPublishApproval

revision = "0011_user_desktops"
down_revision = "0010_local_teammates"
branch_labels = None
depends_on = None


def upgrade():
    for model in (UserDesktop, TaskPrincipal, ComputerPublishApproval):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    pass  # Preserve computer and task ownership pins.
