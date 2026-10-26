"""Add persistent provider bindings, disks and private ingress records.

Revision ID: 0003_computer_providers
Revises: 0002_reconcile
"""
from alembic import op

from app.persistence.models import (ComputerControlContext, ComputerDisk, ComputerEndpoint,
                                    ComputerGateway, ComputerScope, TaskComputerBinding)

revision = "0003_computer_providers"
down_revision = "0002_reconcile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Earlier revisions use current metadata on fresh installs. Existing databases
    # already at 0002 still need these tables without rewriting their saved VMs.
    for model in (ComputerScope, ComputerControlContext, TaskComputerBinding,
                  ComputerDisk, ComputerGateway, ComputerEndpoint):
        model.__table__.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # Provider identities and disks must survive a code rollback.
    pass
