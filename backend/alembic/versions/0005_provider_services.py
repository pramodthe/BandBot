"""Versioned provider configuration and immutable work bindings."""
from alembic import op
from app.providers import models

revision = "0005_provider_services"
down_revision = "0004_document_blobs"
branch_labels = None
depends_on = None

def upgrade():
    for name in ("ProviderConnection", "ProviderRevision", "ServiceSelection", "ServiceSelectionHistory", "TaskServiceBinding",
                 "DocumentServiceBinding", "ProviderBlock", "ProviderOperation", "DocumentProviderIndex", "ServiceChange", "ProviderUsage", "ProviderDocumentCollection", "ProviderDocumentUsage", "ProviderIntegrationSession"):
        getattr(models, name).__table__.create(op.get_bind(), checkfirst=True)

def downgrade():
    pass  # Retain provider ownership and saved credentials for safe code rollback.
