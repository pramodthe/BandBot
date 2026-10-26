"""Additive provider schema; credentials never appear in public snapshots."""
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import JSON, Column, Text
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel

def now():
    return datetime.now(timezone.utc)

class EncryptedConfig(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        import json
        from app.auth import encrypt_secret
        return encrypt_secret(json.dumps(value or {}))

    def process_result_value(self, value, dialect):
        import json
        from app.auth import decrypt_secret
        return json.loads(decrypt_secret(value)) if value else {}

class ProviderConnection(SQLModel, table=True):
    id: str = Field(primary_key=True)
    provider: str
    label: str
    current_revision: int = 1
    status: str = "active"  # active | retired | revoked
    created_by: int | None = None
    created_at: datetime = Field(default_factory=now)

class ProviderRevision(SQLModel, table=True):
    connection_id: str = Field(primary_key=True, foreign_key="providerconnection.id")
    revision: int = Field(primary_key=True)
    secret_enc: str = ""
    config: dict[str, Any] = Field(default_factory=dict, sa_column=Column(EncryptedConfig))
    verification: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    origin: str = "user"
    created_at: datetime = Field(default_factory=now)

class ServiceSelection(SQLModel, table=True):
    scope: str = Field(primary_key=True)  # workspace | agent:<id> | computer:<Band ID>:<channel>
    version: int = 1
    bindings: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    updated_at: datetime = Field(default_factory=now)

class ServiceSelectionHistory(SQLModel, table=True):
    scope: str = Field(primary_key=True)
    version: int = Field(primary_key=True)
    bindings: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

class TaskServiceBinding(SQLModel, table=True):
    task_id: str = Field(primary_key=True)
    bindings: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    runtime_version: int = 1
    connected_secret_enc: str = ""

class DocumentServiceBinding(SQLModel, table=True):
    document_id: str = Field(primary_key=True)
    bindings: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

class ProviderBlock(SQLModel, table=True):
    task_id: str = Field(primary_key=True)
    service: str
    code: str
    connection_id: str = ""
    detail: str = ""

class ProviderOperation(SQLModel, table=True):
    id: str = Field(primary_key=True)
    task_id: str = Field(index=True)
    service: str
    binding: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = "pending"  # pending | completed | uncertain | failed
    result: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=now)

class DocumentProviderIndex(SQLModel, table=True):
    document_id: str = Field(primary_key=True)
    binding_hash: str = Field(primary_key=True)
    binding: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = "queued"
    upload_intent: str = ""
    file_id: str = ""
    index_file_id: str = ""
    store_id: str = ""
    error: str = ""

class ServiceChange(SQLModel, table=True):
    id: str = Field(primary_key=True)
    scope: str = "workspace"
    expected_version: int
    bindings: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    clear_computer_overrides: bool = False
    status: str = "building"  # building | ready | applied | failed | stale
    error: str = ""
    created_at: datetime = Field(default_factory=now)

class ProviderDocumentCollection(SQLModel, table=True):
    channel_id: int = Field(primary_key=True)
    binding_hash: str = Field(primary_key=True)
    binding: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    store_id: str = ""
    state: str = "new"

class ProviderDocumentUsage(SQLModel, table=True):
    usage_id: str = Field(primary_key=True)
    provider: str
    connection_id: str = ""
    revision: int = 0

class ProviderUsage(SQLModel, table=True):
    usage_id: int = Field(primary_key=True)
    provider: str
    connection_id: str = ""
    revision: int = 0
    service: str = "llm"

class ProviderIntegrationSession(SQLModel, table=True):
    connection_id: str = Field(primary_key=True)
    revision: int = Field(primary_key=True)
    user_id: str
    session_id: str
