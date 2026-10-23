"""Durable image display metadata, including existing local image receipts."""
from alembic import op
from app.persistence.models import ImageGeneration, ImageGenerationDisplay

revision = "0006_image_display"
down_revision = "0005_provider_services"
branch_labels = None
depends_on = None


def upgrade():
    for model in (ImageGeneration, ImageGenerationDisplay):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    pass  # Preserve saved image history on code rollback.
