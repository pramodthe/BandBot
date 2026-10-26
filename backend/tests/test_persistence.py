from uuid import uuid4

import pytest

from app.config import sqlalchemy_url
from app import documents
from app.persistence.models import Document, db, engine, init_db


def test_sqlite_writers_wait_instead_of_failing_immediately():
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
    assert mode == "wal"
    assert timeout == 30000


def test_postgres_urls_keep_their_query_and_driver():
    raw = "postgres://user:secret@db.example:25060/band?sslmode=require"
    assert sqlalchemy_url(raw) == "postgresql+psycopg://user:secret@db.example:25060/band?sslmode=require"
    assert sqlalchemy_url("sqlite:///band.db") == "sqlite:///band.db"


def test_document_bytes_live_in_the_database(monkeypatch):
    monkeypatch.setattr(documents, "database_files", lambda: True)
    init_db()
    doc = Document(id=uuid4().hex, channel_id=1, uploaded_by=0, name="note.txt",
                   media_type="text/plain", size=5, status="queued")
    with db() as session:
        assert documents.save_original(session, doc, b"hello") is False
        session.commit()
    assert documents.read_original(doc) == b"hello"
    assert not documents.path_for(doc).exists()
    documents.delete_original(doc)
    with pytest.raises(FileNotFoundError):
        documents.read_original(doc)
