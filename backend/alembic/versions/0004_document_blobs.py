"""Store original document bytes in the application database when DOCUMENTS_PATH=database.

Revision ID: 0004_document_blobs
Revises: 0003_computer_providers
"""
from alembic import op

from app.persistence.models import DocumentBlob

revision = "0004_document_blobs"
down_revision = "0003_computer_providers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    DocumentBlob.__table__.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    DocumentBlob.__table__.drop(op.get_bind(), checkfirst=True)
