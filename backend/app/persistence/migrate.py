"""Versioned schema upgrades for the application database.

Existing installations created with ``create_all`` are stamped at the baseline and then
receive later revisions. Fresh databases run every revision. Checkpoints stay in their own
SQLite file; this module only migrates ``DATABASE_URL``.
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from app.config import BACKEND_DIR, settings, sqlalchemy_url
from app.persistence.models import engine

BASELINE = "0001_baseline"


def alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", sqlalchemy_url(settings.database_url).replace("%", "%%"))
    return cfg


def upgrade_database(bind: Engine | None = None) -> None:
    """Bring ``bind`` (or the application engine) to the current revision."""
    target = bind or engine
    from filelock import FileLock
    import hashlib
    identity = target.url.render_as_string(hide_password=False)
    root = Path(target.url.database).resolve().parent if target.url.drivername.startswith("sqlite") else Path(settings.checkpoint_path).resolve().parent
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / ("schema-" + hashlib.sha256(identity.encode()).hexdigest()[:16] + ".lock")), timeout=120):
        _upgrade_locked(target)

def _upgrade_locked(target):
    cfg = alembic_config()
    # ConfigParser interprets percent signs; URLs can contain encoded drive letters
    # on Windows and percent-encoded PostgreSQL credentials.
    cfg.set_main_option("sqlalchemy.url", target.url.render_as_string(hide_password=False).replace("%", "%%"))
    with target.connect() as connection:
        cfg.attributes["connection"] = connection
        tables = set(inspect(connection).get_table_names())
        if connection.in_transaction():
            connection.commit()
        if "user" in tables and "alembic_version" not in tables:
            command.stamp(cfg, BASELINE)
            if connection.in_transaction():
                connection.commit()
        command.upgrade(cfg, "head")
        if connection.in_transaction():
            connection.commit()


def backup_sqlite(source: Path, destination: Path) -> None:
    """Copy one SQLite database, including a WAL snapshot, without changing the source."""
    import sqlite3

    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as origin, sqlite3.connect(destination) as copy:
        origin.backup(copy)


def backup_workspace(destination: Path) -> None:
    """Copy the application database, checkpoint database, and document files."""
    destination.mkdir(parents=True, exist_ok=True)
    url = settings.database_url
    if url.startswith("sqlite:///"):
        backup_sqlite(Path(url.removeprefix("sqlite:///")), destination / "app.db")
    checkpoint = Path(settings.checkpoint_path)
    if checkpoint.exists():
        backup_sqlite(checkpoint, destination / "checkpoints.db")
    documents = Path(settings.documents_path)
    if documents.exists():
        import shutil
        shutil.copytree(documents, destination / "documents", dirs_exist_ok=True)
