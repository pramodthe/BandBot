"""Add tables introduced after the create_all baseline.

``create_all`` is idempotent: a fresh database already has these tables, and an
installation stamped at the baseline gains only the missing ones. Existing rows
are not rewritten.

Revision ID: 0002_reconcile
Revises: 0001_baseline
Create Date: 2026-10-21
"""
from alembic import op
from sqlmodel import SQLModel

import app.persistence.models  # noqa: F401

revision = "0002_reconcile"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    SQLModel.metadata.create_all(op.get_bind())


def downgrade() -> None:
    pass
