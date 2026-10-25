"""Track local coordinators and their app teammates without native registration."""
from alembic import op
from app.persistence.models import LocalTeammate

revision = "0010_local_teammates"
down_revision = "0009_band_platform_transport"
branch_labels = None
depends_on = None


def upgrade():
    LocalTeammate.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    pass  # Preserve ownership of existing app teammates.
