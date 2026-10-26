"""Track externally owned Band identities connected with agent API keys."""
from alembic import op
from app.persistence.models import BandAgentLink

revision = "0007_band_agent_links"
down_revision = "0006_image_display"
branch_labels = None
depends_on = None


def upgrade():
    BandAgentLink.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    pass  # Preserve connection ownership on rollback.
