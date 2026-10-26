"""Baseline application schema.

Existing databases created before Alembic are stamped here and are not rebuilt.
A fresh database creates every current table.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-21
"""
from alembic import op
from sqlmodel import SQLModel

import app.persistence.models  # noqa: F401

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    SQLModel.metadata.create_all(op.get_bind())


def downgrade() -> None:
    pass
