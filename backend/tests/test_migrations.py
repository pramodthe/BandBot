"""Alembic upgrades an empty database and a pre-migration SQLite file the same way."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session

from app.config import settings
from app.persistence.migrate import backup_sqlite, backup_workspace, upgrade_database
from app.persistence.models import AgentProfile, AgentTask, Channel, ChannelMember, Message, User


def test_fresh_database_reaches_head(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    upgrade_database(engine)
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == "0006_image_display"
    assert {"user", "auth_session", "contact_block", "worker_heartbeat", "client_message"} <= tables


def test_existing_create_all_database_keeps_its_rows(tmp_path: Path):
    path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(text(
            """
            CREATE TABLE user (
                id INTEGER PRIMARY KEY,
                username VARCHAR NOT NULL UNIQUE,
                display_name VARCHAR NOT NULL,
                password_hash VARCHAR NOT NULL,
                created_at DATETIME NOT NULL
            )
            """
        ))
        connection.execute(text(
            "INSERT INTO user (username, display_name, password_hash, created_at) "
            "VALUES ('legacy', 'Legacy', 'kept-hash', '2020-01-01 00:00:00')"
        ))
    upgrade_database(engine)
    with engine.connect() as connection:
        row = connection.execute(text("SELECT password_hash FROM user WHERE username = 'legacy'")).one()
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        tables = set(inspect(connection).get_table_names())
    assert row[0] == "kept-hash"
    assert version == "0006_image_display"
    assert "auth_session" in tables and "contact_block" in tables


def test_backup_restores_messages_membership_tasks_checkpoints_and_documents(tmp_path: Path):
    app_path = tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{app_path}")
    upgrade_database(engine)
    with Session(engine) as session:
        user = User(username="restore", display_name="Restore", password_hash="hash")
        session.add(user)
        session.flush()
        channel = Channel(name="restore-room", topic="kept", created_by=user.id)
        session.add(channel)
        session.flush()
        session.add(ChannelMember(channel_id=channel.id, member_type="human", member_id=user.id))
        session.add(Message(channel_id=channel.id, author_type="human", author_id=user.id, author_name="Restore", content="synthetic"))
        profile = AgentProfile(name="Restore Bot", handle="restore-bot", band_agent_id="restore-bot", band_api_key_enc="enc")
        session.add(profile)
        session.flush()
        session.add(AgentTask(id="task-restore", source_key="restore-key", channel_id=channel.id, agent_id=profile.id,
                              room_id="", prompt="hello", status="queued"))
        session.commit()
    checkpoint = tmp_path / "checkpoints.db"
    with sqlite3.connect(checkpoint) as connection:
        connection.execute("CREATE TABLE checkpoint (thread_id TEXT PRIMARY KEY, payload TEXT)")
        connection.execute("INSERT INTO checkpoint VALUES ('task-restore', 'saved')")
    documents = tmp_path / "documents"
    documents.mkdir()
    (documents / "note.txt").write_text("synthetic document", encoding="utf-8")
    previous = (settings.database_url, settings.checkpoint_path, settings.documents_path)
    object.__setattr__(settings, "database_url", f"sqlite:///{app_path}")
    object.__setattr__(settings, "checkpoint_path", str(checkpoint))
    object.__setattr__(settings, "documents_path", str(documents))
    destination = tmp_path / "backup"
    try:
        backup_workspace(destination)
    finally:
        object.__setattr__(settings, "database_url", previous[0])
        object.__setattr__(settings, "checkpoint_path", previous[1])
        object.__setattr__(settings, "documents_path", previous[2])
    restored = tmp_path / "restored.db"
    backup_sqlite(destination / "app.db", restored)
    with sqlite3.connect(restored) as connection:
        message = connection.execute("SELECT content FROM message WHERE content = 'synthetic'").fetchone()
        member = connection.execute("SELECT member_type FROM channelmember").fetchone()
        task = connection.execute("SELECT status FROM agenttask WHERE id = 'task-restore'").fetchone()
    assert message[0] == "synthetic"
    assert member[0] == "human"
    assert task[0] == "queued"
    with sqlite3.connect(destination / "checkpoints.db") as connection:
        saved = connection.execute("SELECT payload FROM checkpoint").fetchone()
    assert saved[0] == "saved"
    assert (destination / "documents" / "note.txt").read_text(encoding="utf-8") == "synthetic document"


def test_database_at_previous_head_gains_provider_tables_without_losing_vm(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'providers.db'}")
    upgrade_database(engine)
    provider_tables = ["computerscope", "computercontrolcontext", "taskcomputerbinding",
                       "computerdisk", "computergateway", "computerendpoint"]
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO tasksandbox (task_id, name, sandbox_id, status, "
                                "latest_image, frame_id, width, height, last_action, updated_at) "
                                "VALUES ('kept', 'kept-vm', 'same-vm', 'stopped', '', '', 0, 0, '', CURRENT_TIMESTAMP)"))
        for table in provider_tables:
            connection.execute(text(f"DROP TABLE {table}"))
        connection.execute(text("UPDATE alembic_version SET version_num = '0002_reconcile'"))
    upgrade_database(engine)
    upgrade_database(engine)  # API and worker startup both remain idempotent.
    with engine.connect() as connection:
        assert set(provider_tables) <= set(inspect(connection).get_table_names())
        assert connection.execute(text("SELECT sandbox_id FROM tasksandbox WHERE task_id = 'kept'")).scalar() == "same-vm"
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0006_image_display"


def test_alembic_url_preserves_percent_encoded_credentials(monkeypatch):
    from app.persistence.migrate import alembic_config
    url = "postgresql+psycopg://user:password%25encoded@localhost/band"
    from types import SimpleNamespace
    import app.persistence.migrate as migrate
    monkeypatch.setattr(migrate, "settings", SimpleNamespace(database_url=url))
    assert alembic_config().get_main_option("sqlalchemy.url") == url


def test_image_display_upgrade_preserves_existing_image_receipts(tmp_path):
    from sqlalchemy import text, inspect
    engine = create_engine(f"sqlite:///{tmp_path / 'images.db'}")
    upgrade_database(engine)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO imagegeneration (id, task_id, status, document_id, model, error, created_at) VALUES ('kept', 'task', 'completed', 'png', 'offline', '', '2026-10-22')"))
        connection.execute(text("DROP TABLE imagegenerationdisplay"))
        connection.execute(text("UPDATE alembic_version SET version_num = '0005_provider_services'"))
    upgrade_database(engine)
    upgrade_database(engine)
    with engine.connect() as connection:
        assert "imagegenerationdisplay" in inspect(connection).get_table_names()
        assert connection.execute(text("SELECT document_id FROM imagegeneration WHERE id = 'kept'")).scalar() == "png"
