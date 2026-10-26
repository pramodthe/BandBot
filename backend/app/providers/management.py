"""Validated configuration changes, revision checks, and dependency-safe retirement."""
from copy import deepcopy
import hashlib
import json
import re
from uuid import uuid4
from sqlalchemy import update
from sqlmodel import select
from app.auth import encrypt_secret
from app.persistence.models import AgentTask, TaskComputerBinding, TaskSandbox, Document, utcnow
from app.providers.models import (ProviderConnection, ProviderRevision, ServiceSelection, ServiceSelectionHistory,
                                  TaskServiceBinding, DocumentProviderIndex, ServiceChange)
from app.providers.registry import SERVICES, manifest
from app.providers.connections import resolve

class ConfigurationConflict(ValueError):
    pass

def clean_config(provider: str, config: dict) -> dict:
    definition = manifest(provider)
    if set(config) - set(definition.fields):
        raise ValueError("Unsupported configuration fields")
    value = deepcopy(config)
    for name in set(definition.fields) - {"models", "context_window", "disk_gib", "timeout", "pause_retention"}:
        if name in value and (not isinstance(value[name], str) or len(value[name]) > 2048):
            raise ValueError(f"Enter a valid {name}")
    if value.get("image_model") == "":
        value.pop("image_model")
    if "image_model" in value and not re.fullmatch(r"[a-zA-Z0-9._:/-]{1,160}", value["image_model"]):
        raise ValueError("Enter a valid image model identifier")
    if "models" in value:
        models = value["models"]
        if not isinstance(models, list) or not (0 if value.get("image_model") else 1) <= len(models) <= 24 or any(not isinstance(m, str) or not re.fullmatch(r"[a-zA-Z0-9._:/-]{1,160}", m) for m in models):
            raise ValueError("Enter 1–24 valid model identifiers")
        value["models"] = list(dict.fromkeys(models))
    if provider == "compatible":
        from app.providers.endpoint import validate_base
        value["base_url"] = validate_base(value.get("base_url", ""))
        if type(value.get("context_window")) is not int or not 4096 <= value["context_window"] <= 2000000:
            raise ValueError("Set the endpoint model context window between 4096 and 2000000 tokens")
    if provider == "tenki":
        if not isinstance(value.get("image"), str) or not 1 <= len(value["image"]) <= 300:
            raise ValueError("A prepared Tenki desktop image is required")
        value.setdefault("sharing", "user")
        if value["sharing"] not in {"user", "workspace", "agent_channel"}:
            raise ValueError("Choose user, workspace or agent_channel sharing")
        for name, default, low, high in (("disk_gib", 1, 1, 50), ("timeout", 1200, 60, 3600), ("pause_retention", 604800, 3600, 2592000)):
            value.setdefault(name, default)
            if type(value[name]) is not int or not low <= value[name] <= high:
                raise ValueError(f"Invalid {name}")
    if provider == "e2b":
        value.setdefault("template", "desktop")
        value.setdefault("timeout", 1200)
        if not isinstance(value["template"], str) or not value["template"].strip() or type(value["timeout"]) is not int or not 60 <= value["timeout"] <= 3600:
            raise ValueError("Enter a desktop template and a timeout between 60 and 3600 seconds")
    return value

def create_connection(session, *, provider: str, label: str, key: str, config: dict, user_id: int):
    definition = manifest(provider)
    if definition.secret_required and not key.strip():
        raise ValueError("An API key is required")
    if not label.strip() or len(label) > 80:
        raise ValueError("Enter a connection name of up to 80 characters")
    config = clean_config(provider, config)
    identity = uuid4().hex
    if provider in {"tenki", "e2b"}:
        config["computer_namespace"] = identity[:12] + "-r1"
    row = ProviderConnection(id=identity, provider=provider, label=label.strip(), created_by=user_id)
    session.add(row)
    session.flush()
    revision = ProviderRevision(connection_id=identity, revision=1, config=config,
        secret_enc=encrypt_secret(json.dumps({"api_key": key.strip()})) if key.strip() else "")
    session.add(revision)
    session.flush()
    return row

def rotate(session, row, *, expected_revision: int, key: str | None, config: dict | None):
    if row.status != "active" or row.current_revision != expected_revision:
        raise ConfigurationConflict("This connection changed. Reload before editing it.")
    old = session.get(ProviderRevision, (row.id, expected_revision))
    value = clean_config(row.provider, config if config is not None else {k:v for k,v in old.config.items() if k in manifest(row.provider).fields})
    revision = expected_revision + 1
    if row.provider in {"tenki", "e2b"}:
        value["computer_namespace"] = row.id[:12] + f"-r{revision}"
    result = session.execute(update(ProviderConnection).where(ProviderConnection.id == row.id,
        ProviderConnection.current_revision == expected_revision, ProviderConnection.status == "active").values(current_revision=revision))
    if result.rowcount != 1:
        raise ConfigurationConflict("This connection changed. Reload before editing it.")
    if key is not None and not key.strip() and manifest(row.provider).secret_required:
        raise ValueError("An API key is required")
    session.add(ProviderRevision(connection_id=row.id, revision=revision, config=value,
        secret_enc=old.secret_enc if key is None else encrypt_secret(json.dumps({"api_key": key.strip()})) if key.strip() else ""))
    session.flush()
    return revision

def validate_bindings(session, bindings: dict):
    if set(bindings) - set(SERVICES):
        raise ValueError("Unknown service")
    identities = sorted({b.get("connection_id") for b in bindings.values() if isinstance(b, dict) and isinstance(b.get("connection_id"), str)})
    if identities:
        session.exec(select(ProviderConnection).where(ProviderConnection.id.in_(identities)).order_by(ProviderConnection.id).with_for_update()).all()
    for service, binding in bindings.items():
        if not isinstance(binding, dict) or set(binding) - {"connection_id", "revision", "provider", "model", "disabled", "hosted_code"}:
            raise ValueError("Invalid service binding")
        if "disabled" in binding and type(binding["disabled"]) is not bool:
            raise ValueError("Invalid disabled flag")
        if binding.get("disabled") and binding != {"disabled": True}:
            raise ValueError("Disabled services cannot include connection fields")
        if binding.get("disabled"):
            continue
        if not isinstance(binding.get("connection_id"), str) or not 1 <= len(binding["connection_id"]) <= 80 or not isinstance(binding.get("provider"), str):
            raise ValueError("Choose a valid provider connection")
        if "model" in binding and (service not in {"llm", "document_analysis", "images"} or not isinstance(binding["model"], str) or not re.fullmatch(r"[a-zA-Z0-9._:/-]{1,160}", binding["model"])):
            raise ValueError("Choose a valid model for this service")
        if type(binding.get("revision")) is not int or binding["revision"] < 1:
            raise ValueError("A connection revision is required")
        if "hosted_code" in binding and (service != "llm" or type(binding["hosted_code"]) is not bool or binding.get("provider") != "openai"):
            raise ValueError("Hosted execution is available only for OpenAI agent models")
        if binding.get("disabled"):
            continue
        connection = resolve(binding, service, session=session)
        row = session.get(ProviderConnection, connection.id)
        if row.status != "active":
            raise ValueError("Retired connections cannot be selected for new work")
        if service not in manifest(connection.provider).services:
            raise ValueError("This provider does not support the selected service")
        if not connection.verification.get("passed") and not connection.verification.get("legacy"):
            raise ValueError("Verify this connection before activation")
        if service in {"llm", "document_analysis"}:
            model = binding.get("model", "")
            tested = connection.verification.get("models", {}).get(model, {})
            if not tested.get("vision") or not tested.get("tools"):
                raise ValueError("Choose a model verified for image inputs and a complete tool round trip")
        if binding.get("hosted_code") and not (connection.verification.get("legacy") or connection.verification.get("models", {}).get(binding.get("model"), {}).get("hosted_code")):
            raise ValueError("Hosted execution requires a verified hosted-execution capability. Use the selected computer for portable calculations.")
        if service == "computer" and not connection.verification.get("legacy") and not connection.verification.get("pause_resume"):
            raise ValueError("Run the isolated computer check before activation")
        if service == "images" and connection.verification.get("image_model") != binding.get("model"):
            raise ValueError("Verify access to the selected image model before activation")
        if service == "images" and not binding.get("model"):
            raise ValueError("Choose an image model")

def apply_selection(session, *, scope: str, expected_version: int, bindings: dict, clear_computer_overrides=False):
    validate_bindings(session, bindings)
    current = session.get(ServiceSelection, scope)
    actual = current.version if current else 0
    if actual != expected_version:
        raise ConfigurationConflict("Settings changed. Reload before applying your selection.")
    if current:
        session.merge(ServiceSelectionHistory(scope=scope, version=actual, bindings=current.bindings))
        result = session.execute(update(ServiceSelection).where(ServiceSelection.scope == scope,
            ServiceSelection.version == expected_version).values(bindings=deepcopy(bindings), version=actual + 1, updated_at=utcnow()))
        if result.rowcount != 1:
            raise ConfigurationConflict("Settings changed. Reload before applying your selection.")
    else:
        session.add(ServiceSelection(scope=scope, version=1, bindings=deepcopy(bindings)))
    if clear_computer_overrides:
        for override in session.exec(select(ServiceSelection).where(ServiceSelection.scope.startswith("computer:"))).all():
            session.merge(ServiceSelectionHistory(scope=override.scope, version=override.version, bindings=override.bindings))
            cleared = session.execute(update(ServiceSelection).where(ServiceSelection.scope == override.scope,
                ServiceSelection.version == override.version).values(bindings={}, version=override.version + 1, updated_at=utcnow()))
            if cleared.rowcount != 1:
                raise ConfigurationConflict("A computer override changed. Reload before replacing overrides.")
    session.flush()

def dependencies(session, identity: str):
    def uses(bindings):
        return any(b.get("connection_id") == identity for b in bindings.values())
    selections = [r.scope for r in session.exec(select(ServiceSelection)).all() if uses(r.bindings)]
    unfinished = 0
    computers = set()
    for row in session.exec(select(TaskServiceBinding)).all():
        if not uses(row.bindings):
            continue
        task = session.get(AgentTask, row.task_id)
        if task and task.status != "completed":
            unfinished += 1
        binding = session.get(TaskComputerBinding, row.task_id)
        if row.bindings.get("computer", {}).get("connection_id") == identity and binding:
            computer = session.get(TaskSandbox, binding.computer_key)
            if computer and computer.sandbox_id and computer.status != "missing":
                computers.add(binding.computer_key)
    resources = sum(1 for r in session.exec(select(DocumentProviderIndex)).all()
        if r.binding.get("connection_id") == identity and any((r.file_id, r.index_file_id, r.store_id)))
    from app.providers.models import DocumentServiceBinding, ProviderDocumentCollection, ProviderOperation
    document_jobs = sum(1 for r in session.exec(select(DocumentServiceBinding)).all()
        if uses(r.bindings) and (doc := session.get(Document, r.document_id)) and doc.status in {"staged", "queued", "processing", "deleting", "error"})
    resources += sum(1 for r in session.exec(select(ProviderDocumentCollection)).all()
        if r.binding.get("connection_id") == identity and (r.store_id or r.state == "creating"))
    pending = sum(1 for r in session.exec(select(ServiceChange)).all() if uses(r.bindings) and r.status == "building")
    pending += sum(1 for r in session.exec(select(ProviderOperation)).all()
        if r.binding.get("connection_id") == identity and (r.status in {"pending", "running", "uncertain"} or r.result.get("cleanup") == "needs_attention"))
    return {"selections": selections, "unfinished_tasks": unfinished, "computers": len(computers),
            "document_resources": resources, "document_jobs": document_jobs, "pending_operations": pending}

def binding_hash(binding: dict):
    return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()[:24]

def clear_agent_overrides(session, profile):
    """A recycled database ID must not inherit a removed teammate's providers."""
    for row in session.exec(select(ServiceSelection)).all():
        if row.scope == f"agent:{profile.id}" or row.scope.startswith(f"computer:{profile.band_agent_id}:"):
            session.merge(ServiceSelectionHistory(scope=row.scope, version=row.version, bindings=row.bindings))
            row.bindings = {}
            row.version += 1
            row.updated_at = utcnow()
            session.add(row)
