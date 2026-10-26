"""Admin-only provider management. Verification runs in the separate worker."""
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr
from sqlmodel import Session, select
from sqlalchemy.exc import IntegrityError
from app.api.deps import admin_user, agent_or_404, channel_or_404, require_task_member, task_or_404
from app.auth import current_user
from app.persistence.models import (User, AgentProfile, AgentRuntime, AgentTask, ChannelMember, Document, TaskDocument,
                                    get_session)
from app.providers.models import (ProviderConnection, ProviderRevision, ServiceSelection, ServiceSelectionHistory,
                                  ProviderOperation, ServiceChange)
from app.providers.registry import catalog
from app.providers.connections import public_connection
from app.providers.management import (create_connection, rotate, apply_selection, validate_bindings,
    dependencies, ConfigurationConflict, binding_hash)
from app.providers.contracts import ProviderError
from app.workspace.access import agent_visible
from app.workspace.audit import record, record_human
from app.workspace.events import bus

router = APIRouter()
admin = admin_user("Only workspace admins can configure providers")


def get_connection(session, identity):
    row = session.exec(select(ProviderConnection).where(ProviderConnection.id == identity).with_for_update()).first()
    if not row:
        raise HTTPException(404, "Connection not found")
    return row


def connection_json(session, row):
    revisions = session.exec(select(ProviderRevision).where(ProviderRevision.connection_id == row.id)
        .order_by(ProviderRevision.revision.desc())).all()
    return {**public_connection(row, revisions), "dependencies": dependencies(session, row.id)}


def safe_error(error):
    return HTTPException(409 if isinstance(error, ConfigurationConflict) else 422, str(error))


@router.get("/api/providers")
def provider_catalog(_user: User = Depends(current_user)):
    return {"providers": catalog()}


@router.get("/api/settings/providers")
def provider_settings(_user: User = Depends(admin), session: Session = Depends(get_session)):
    row = session.get(ServiceSelection, "workspace")
    connections = session.exec(select(ProviderConnection).order_by(ProviderConnection.created_at)).all()
    histories = session.exec(select(ServiceSelectionHistory).where(ServiceSelectionHistory.scope == "workspace")
        .order_by(ServiceSelectionHistory.version.desc()).limit(10)).all()
    return {"version": row.version if row else 0, "bindings": row.bindings if row else {},
            "connections": [connection_json(session, c) for c in connections],
            "operations": [{"id": o.id, "status": o.status, "result": o.result, "connection_id": o.binding.get("connection_id")}
                for o in session.exec(select(ProviderOperation).where(ProviderOperation.service.in_(["verification", "index_cleanup"]))
                    .order_by(ProviderOperation.created_at.desc()).limit(12)).all()],
            "history": [{"version": h.version, "bindings": h.bindings} for h in histories],
            "computer_overrides": sum(bool(row.bindings.get("computer")) for row in session.exec(select(ServiceSelection).where(ServiceSelection.scope.startswith("computer:"))).all()),
            "changes": [{"id": c.id, "status": c.status, "error": c.error} for c in session.exec(
                select(ServiceChange).where(ServiceChange.scope == "workspace").order_by(ServiceChange.created_at.desc()).limit(10)).all()]}


class ConnectionBody(BaseModel):
    provider: str = Field(max_length=40)
    label: str = Field(min_length=1, max_length=80)
    api_key: SecretStr = Field(default=SecretStr(""), max_length=4000)
    config: dict = Field(default_factory=dict)


@router.post("/api/settings/connections", status_code=201)
async def add_connection(body: ConnectionBody, user: User = Depends(admin), session: Session = Depends(get_session)):
    try:
        row = create_connection(session, provider=body.provider, label=body.label, key=body.api_key.get_secret_value(), config=body.config, user_id=user.id)
    except ValueError as error:
        session.rollback()
        raise safe_error(error) from None
    record_human(user, "provider_connection_created", session=session, provider=row.provider, connection_id=row.id)
    session.commit()
    await bus.publish({"type": "provider_settings_changed"})
    return connection_json(session, row)


class RotateBody(BaseModel):
    expected_revision: int = Field(ge=1)
    api_key: SecretStr | None = Field(default=None, max_length=4000)
    config: dict | None = None


@router.patch("/api/settings/connections/{identity}")
async def rotate_connection(identity: str, body: RotateBody, user: User = Depends(admin), session: Session = Depends(get_session)):
    row = get_connection(session, identity)
    try:
        revision = rotate(session, row, expected_revision=body.expected_revision,
            key=body.api_key.get_secret_value() if body.api_key is not None else None, config=body.config)
    except ValueError as error:
        session.rollback()
        raise safe_error(error) from None
    record_human(user, "provider_connection_rotated", session=session, connection_id=identity, revision=revision)
    session.commit()
    session.refresh(row)
    await bus.publish({"type": "provider_settings_changed"})
    return connection_json(session, row)


class VerifyBody(BaseModel):
    revision: int = Field(ge=1)
    live_computer: bool = False


@router.post("/api/settings/connections/{identity}/verify", status_code=202)
def verify_connection(identity: str, body: VerifyBody, user: User = Depends(admin), session: Session = Depends(get_session)):
    row = get_connection(session, identity)
    if row.status != "active" or row.current_revision != body.revision:
        raise HTTPException(409, "This connection changed. Reload before verification.")
    pending = session.exec(select(ProviderOperation).where(ProviderOperation.service == "verification",
        ProviderOperation.status.in_(["pending", "running", "uncertain"]))).all()
    if any(p.binding.get("connection_id") == identity for p in pending):
        raise HTTPException(409, "This connection has a pending or uncertain check. Review or clean up that operation first.")
    job = ProviderOperation(id=uuid4().hex, task_id="", service="verification",
        binding={"connection_id": identity, "revision": body.revision, "provider": row.provider},
        result={"live_computer": body.live_computer})
    session.add(job)
    record_human(user, "provider_verification_requested", session=session, connection_id=identity,
                 revision=body.revision, live_computer=body.live_computer)
    session.commit()
    return {"id": job.id, "status": job.status}


@router.get("/api/settings/provider-operations/{identity}")
def operation_status(identity: str, _user: User = Depends(admin), session: Session = Depends(get_session)):
    row = session.get(ProviderOperation, identity)
    if not row or row.service != "verification":
        raise HTTPException(404, "Verification not found")
    return {"id": row.id, "status": row.status, "result": row.result}


@router.post("/api/settings/provider-operations/{identity}/cleanup", status_code=202)
def cleanup_verification(identity: str, user: User = Depends(admin), session: Session = Depends(get_session)):
    row = session.get(ProviderOperation, identity)
    if not row or row.service != "verification" or row.status not in {"failed", "uncertain"} or not row.result.get("probe_name"):
        raise HTTPException(409, "No interrupted computer test needs cleanup")
    row.result = {**row.result, "cleanup_only": True}
    row.status = "pending"
    session.add(row)
    record_human(user, "provider_probe_cleanup_requested", session=session, operation_id=identity)
    session.commit()
    return {"id": row.id, "status": row.status}


class StateBody(BaseModel):
    expected_status: str | None = None
    action: str = Field(pattern="^(retire|revoke|restore)$")
    expected_revision: int = Field(ge=1)


@router.post("/api/settings/connections/{identity}/state")
async def change_state(identity: str, body: StateBody, user: User = Depends(admin), session: Session = Depends(get_session)):
    row = get_connection(session, identity)
    if row.current_revision != body.expected_revision or body.expected_status is not None and row.status != body.expected_status:
        raise HTTPException(409, "This connection changed. Reload first.")
    usage = dependencies(session, identity)
    if body.action == "retire" and usage["selections"]:
        raise HTTPException(409, "Reassign or disable services and overrides before retiring this connection")
    if row.status == "revoked" and body.action != "revoke":
        raise HTTPException(409, "A revoked connection requires a new verified connection")
    row.status = {"retire": "retired", "revoke": "revoked", "restore": "active"}[body.action]
    session.add(row)
    record_human(user, "provider_connection_" + body.action, session=session, connection_id=identity)
    session.commit()
    await bus.publish({"type": "provider_settings_changed"})
    return connection_json(session, row)


@router.delete("/api/settings/connections/{identity}")
async def remove_connection(identity: str, user: User = Depends(admin), session: Session = Depends(get_session)):
    row = get_connection(session, identity)
    usage = dependencies(session, identity)
    if row.status != "retired" or any(usage.values()):
        raise HTTPException(409, "Retire this connection after releasing its services, unfinished tasks, computers and remote document resources")
    for revision in session.exec(select(ProviderRevision).where(ProviderRevision.connection_id == identity)).all():
        session.delete(revision)
    session.flush()
    session.delete(row)
    record_human(user, "provider_connection_removed", session=session, connection_id=identity)
    session.commit()
    await bus.publish({"type": "provider_settings_changed"})
    return {"ok": True}


class SelectionBody(BaseModel):
    expected_version: int = Field(ge=0)
    bindings: dict[str, dict] = Field(default_factory=dict)
    clear_computer_overrides: bool = False


def preview(session, body):
    from app.providers.registry import SERVICES
    body.bindings = {service: body.bindings.get(service, {"disabled": True}) for service in SERVICES} | {k:v for k,v in body.bindings.items() if k not in SERVICES}
    validate_bindings(session, body.bindings)
    current = session.get(ServiceSelection, "workspace")
    if (current.version if current else 0) != body.expected_version:
        raise ConfigurationConflict("Settings changed. Reload before applying your selection.")
    changed = [s for s in body.bindings if (current.bindings if current else {}).get(s) != body.bindings[s]]
    count = len(session.exec(select(AgentTask.id).where(AgentTask.status.in_(["queued", "running", "waiting"]))).all())
    old_retrieval = (current.bindings if current else {}).get("retrieval", {})
    reindex = old_retrieval != body.bindings.get("retrieval", {}) and not body.bindings.get("retrieval", {}).get("disabled")
    documents = len(session.exec(select(Document.id).where(Document.status == "ready")).all()) if reindex else 0
    return {"changed_services": changed, "preserved_tasks": count, "documents_to_reindex": documents,
            "computer_overrides_cleared": body.clear_computer_overrides,
            "note": "New work uses this selection. Existing tasks, computers, files and approvals retain their original bindings."}


@router.post("/api/settings/services/preview")
def preview_selection(body: SelectionBody, _user: User = Depends(admin), session: Session = Depends(get_session)):
    try:
        return preview(session, body)
    except (ValueError, ProviderError) as error:
        raise safe_error(error) from None


@router.put("/api/settings/services")
async def activate_selection(body: SelectionBody, user: User = Depends(admin), session: Session = Depends(get_session)):
    try:
        impact = preview(session, body)
        if impact["documents_to_reindex"]:
            change = ServiceChange(id=uuid4().hex, expected_version=body.expected_version, bindings=body.bindings,
                                   clear_computer_overrides=body.clear_computer_overrides)
            session.add(change)
            record_human(user, "provider_selection_staged", session=session, change_id=change.id,
                         services=impact["changed_services"])
            session.commit()
            return {"status": "building", "change_id": change.id, **impact}
        apply_selection(session, scope="workspace", expected_version=body.expected_version, bindings=body.bindings,
                        clear_computer_overrides=body.clear_computer_overrides)
    except (ValueError, ProviderError) as error:
        session.rollback()
        raise safe_error(error) from None
    record_human(user, "provider_selection_activated", session=session, services=impact["changed_services"],
                 clear_computer_overrides=body.clear_computer_overrides)
    session.commit()
    await bus.publish({"type": "provider_settings_changed"})
    return {"status": "applied", **impact}


@router.put("/api/settings/agents/{agent_id}/services")
async def agent_selection(agent_id: int, body: SelectionBody, user: User = Depends(admin), session: Session = Depends(get_session)):
    agent = agent_or_404(session, agent_id)
    if not agent_visible(session, agent, user.id):
        raise HTTPException(404, "Agent not found")
    if session.get(AgentRuntime, agent_id):
        raise HTTPException(422, "Connected agents retain their personal runtime connection")
    if set(body.bindings) - {"llm"}:
        raise HTTPException(422, "Agent settings override only the LLM service")
    try:
        apply_selection(session, scope=f"agent:{agent_id}", expected_version=body.expected_version, bindings=body.bindings)
    except (ValueError, ProviderError) as error:
        session.rollback()
        raise safe_error(error) from None
    record_human(user, "agent_provider_selected", session=session, agent_id=agent_id)
    session.commit()
    await bus.publish({"type": "provider_settings_changed"})
    return {"version": body.expected_version + 1, "bindings": body.bindings}


@router.get("/api/settings/agents/{agent_id}/services")
def get_agent_selection(agent_id: int, user: User = Depends(admin), session: Session = Depends(get_session)):
    agent = agent_or_404(session, agent_id)
    if not agent_visible(session, agent, user.id):
        raise HTTPException(404, "Agent not found")
    row = session.get(ServiceSelection, f"agent:{agent_id}")
    return {"version": row.version if row else 0, "bindings": row.bindings if row else {}}


@router.post("/api/tasks/{task_id}/restart-with-providers")
async def restart_task(task_id: str, user: User = Depends(current_user), session: Session = Depends(get_session)):
    from app.providers.bindings import bindings_for, selected
    from app.tasks.queue import serialize_task
    task = task_or_404(session, task_id)
    require_task_member(session, task, user)
    if task.status not in {"failed", "cancelled"}:
        raise HTTPException(409, "Cancel or finish the original task before starting a replacement")
    profile = session.get(AgentProfile, task.agent_id)
    original_identity = bindings_for(task).get("_agent", {}).get("band_id")
    if not profile or original_identity and original_identity != profile.band_agent_id or not session.get(ChannelMember, (task.channel_id, "agent", task.agent_id)):
        raise HTTPException(409, "The original teammate no longer belongs to this conversation")
    source = f"provider-restart:{task_id}:{user.id}:" + binding_hash(selected(session, task.agent_id, task.channel_id))
    existing = session.exec(select(AgentTask).where(AgentTask.source_key == source)).first()
    if existing:
        return serialize_task(existing)
    context = (f"This explicitly replaces task {task_id}, whose checkpoint and approvals are retained separately. "
        "It may already have completed external actions. Inspect current channel history and computer files before continuing; "
        "never repeat a confirmed or uncertain external action without checking and getting fresh approval.\n\n" + task.context)
    replacement = AgentTask(id=uuid4().hex, source_key=source, agent_id=task.agent_id,
        channel_id=task.channel_id, room_id=task.room_id, prompt=task.prompt, context=context, reply_to=task.reply_to,
        progress="New task using current provider settings; original checkpoint preserved")
    session.add(replacement)
    for link in session.exec(select(TaskDocument).where(TaskDocument.task_id == task_id)).all():
        session.add(TaskDocument(task_id=replacement.id, document_id=link.document_id))
    # The durable source relationship is independent of teammate hand-off limits.
    session.add(ProviderOperation(id="restart:" + replacement.id, task_id=replacement.id, service="restart",
        status="completed", result={"original_task_id": task_id}))
    record_human(user, "task_provider_restart", session=session, original_task_id=task_id,
                 replacement_task_id=replacement.id)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.exec(select(AgentTask).where(AgentTask.source_key == source)).first()
        if not existing:
            raise
        return serialize_task(existing)
    return serialize_task(replacement)


@router.post("/api/settings/connections/{identity}/release-indexes", status_code=202)
def release_indexes(identity: str, user: User = Depends(admin), session: Session = Depends(get_session)):
    row = get_connection(session, identity)
    usage = dependencies(session, identity)
    if row.status != "retired" or usage["selections"] or usage["unfinished_tasks"] or usage["document_jobs"]:
        raise HTTPException(409, "Retire and reassign this connection, then finish existing work before releasing remote indexes")
    operation = ProviderOperation(id=uuid4().hex, task_id="", service="index_cleanup",
        binding={"connection_id": identity, "revision": row.current_revision, "provider": row.provider})
    session.add(operation)
    record_human(user, "provider_index_cleanup_requested", session=session, connection_id=identity)
    session.commit()
    return {"id": operation.id, "status": operation.status}


@router.post("/api/settings/provider-operations/{identity}/acknowledge")
def acknowledge_check(identity: str, user: User = Depends(admin), session: Session = Depends(get_session)):
    row = session.get(ProviderOperation, identity)
    if not row or row.service != "verification" or row.status != "uncertain" or row.result.get("probe_name"):
        raise HTTPException(409, "Only an interrupted check without computer resources can be acknowledged")
    row.status = "failed"
    row.result = {**row.result, "acknowledged": True}
    session.add(row)
    record_human(user, "provider_check_acknowledged", session=session, operation_id=identity)
    session.commit()
    return {"id": identity, "status": row.status}


@router.get("/api/settings/computers/{agent_id}/{channel_id}/services")
def computer_selection(agent_id: int, channel_id: int, user: User = Depends(admin), session: Session = Depends(get_session)):
    profile = agent_or_404(session, agent_id)
    channel_or_404(session, channel_id, user)
    row = session.get(ServiceSelection, f"computer:{profile.band_agent_id}:{channel_id}")
    return {"version": row.version if row else 0, "bindings": row.bindings if row else {}}


@router.put("/api/settings/computers/{agent_id}/{channel_id}/services")
async def select_computer(agent_id: int, channel_id: int, body: SelectionBody, user: User = Depends(admin), session: Session = Depends(get_session)):
    profile = agent_or_404(session, agent_id)
    channel_or_404(session, channel_id, user)
    if not agent_visible(session, profile, user.id):
        raise HTTPException(404, "Agent not found")
    if set(body.bindings) - {"computer"}:
        raise HTTPException(422, "Override only the computer service")
    try:
        apply_selection(session, scope=f"computer:{profile.band_agent_id}:{channel_id}", expected_version=body.expected_version, bindings=body.bindings)
    except (ValueError, ProviderError) as error:
        session.rollback()
        raise safe_error(error) from None
    # Workspace-level event: the channel is detail, not the event's channel scope.
    record("computer_provider_selected", actor_type="human", actor_id=user.id, actor_name=user.display_name,
           detail={"agent_id": agent_id, "channel_id": channel_id}, session=session)
    session.commit()
    await bus.publish({"type": "provider_settings_changed"})
    return {"version": body.expected_version + 1, "bindings": body.bindings}


@router.post("/api/settings/document-indexes/retry")
def retry_indexes(user: User = Depends(admin), session: Session = Depends(get_session)):
    from app.providers.models import DocumentProviderIndex
    count = 0
    for row in session.exec(select(DocumentProviderIndex).where(DocumentProviderIndex.status == "error")).all():
        row.status = "queued"
        row.error = ""
        session.add(row)
        count += 1
    record_human(user, "document_indexes_retried", session=session, count=count)
    session.commit()
    return {"queued": count}
