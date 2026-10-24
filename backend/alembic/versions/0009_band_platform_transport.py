"""Pin Agent API room transport and app teammates to their owning connection."""
from alembic import op
from app.persistence.models import BandPlatformRoom, PlatformTeammate

revision = "0009_band_platform_transport"
down_revision = "0008_user_band_connections"
branch_labels = None
depends_on = None


def upgrade():
    for model in (BandPlatformRoom, PlatformTeammate):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    pass  # Preserve transport identity pins and saved work.
