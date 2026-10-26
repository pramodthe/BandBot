"""Store each user's encrypted Band platform connection."""
from alembic import op
from app.persistence.models import UserBandConnection

revision = "0008_user_band_connections"
down_revision = "0007_band_agent_links"
branch_labels = None
depends_on = None


def upgrade():
    UserBandConnection.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    pass  # Keep account credentials on rollback.
