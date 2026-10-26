"""A user-owned host with independently addressed agent desktop sessions."""
import hashlib

from e2b import NotFoundException
from sqlmodel import select

from app.computer.identity import user_keys, workspace_id
from app.computer.providers import ComputerMissing
from app.persistence.models import (as_utc, AgentProfile, AgentTask, ComputerViewLease,
                                    TaskComputerBinding, UserDesktop, db, utcnow)


def ports(display):
    return 20000 + display * 2, 20001 + display * 2


def host_key(desktop_key):
    with db() as session:
        row = session.get(UserDesktop, desktop_key)
        return row.host_key if row else desktop_key


def reserve_desktop(computer, host, user_id):
    # Caller owns the cross-process host lock.
    with db() as session:
        row = session.get(UserDesktop, computer.key)
        if row:
            if row.host_key != host or row.user_id != user_id or row.agent_identity != computer.owner:
                raise RuntimeError("Desktop ownership did not match")
            return row
        used = set(session.exec(select(UserDesktop.display).where(UserDesktop.host_key == host)).all())
        display = next((number for number in range(1, 65) if number not in used), None)
        if display is None:
            from app.computer.desktop import ComputerUnavailable
            raise ComputerUnavailable("This user's computer has reached its desktop limit")
        row = UserDesktop(key=computer.key, host_key=host, user_id=user_id,
                          agent_identity=computer.owner, display=display)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def related_keys(host):
    with db() as session:
        return session.exec(select(UserDesktop.key).where(UserDesktop.host_key == host)).all()


def host_in_use(host, *, excluding=None, force_key=None):
    keys = related_keys(host)
    from app.computer.app_previews import has_active_view
    from app.computer.desktop import active_control
    with db() as session:
        running = session.exec(select(AgentTask.id).join(TaskComputerBinding, TaskComputerBinding.task_id == AgentTask.id)
            .where(TaskComputerBinding.computer_key.in_(keys), AgentTask.status == "running",
                   AgentTask.id != (excluding or ""))).first()
        watched_keys = [key for key in keys if key != force_key]
        leases = session.exec(select(ComputerViewLease).where(ComputerViewLease.key.in_(watched_keys))).all()
    return bool(running or any(as_utc(lease.until) > utcnow()
                              for lease in leases) or any(active_control(key) or has_active_view(key) for key in watched_keys))


async def ensure(computer, *, desktop, create):
    from app.computer.desktop import ComputerUnavailable, save_sandbox, sandbox_record
    from app.computer.e2b import DesktopOptions
    from app.computer.access import task_may_use_private_computer
    with db() as session:
        profile = session.get(AgentProfile, computer.task.agent_id)
        keys = user_keys(session, computer.owner, computer.config)
        if (not profile or not keys or computer.key != keys[1]
                or as_utc(computer.task.created_at) < as_utc(profile.created_at)):
            raise ComputerUnavailable("This task's private computer ownership is no longer valid")
        if create and not task_may_use_private_computer(session, computer.task, keys[2]):
            raise ComputerUnavailable("Only a request from this agent's owner can use their private computer. Ask the owner to start the task.")
    host, _, user_id = keys
    screen = reserve_desktop(computer, host, user_id)
    if computer.sandbox is not None:
        await computer.detach()
    if computer.client is None:
        computer.client = computer.client_factory()
    row = sandbox_record(host)
    if not row and not create:
        raise ComputerUnavailable("This user's computer has not started yet")
    row = row or save_sandbox(host, name="band-tenki-user-" + hashlib.sha256(host.encode()).hexdigest()[:32])
    labels = {"app": "band-slack", "workspace": workspace_id(), "scope": "user", "host_key": host,
              "user": str(user_id), "account_namespace": getattr(computer.config, "computer_namespace", "")}
    try:
        remote = await computer.client.get(row.sandbox_id or row.name, request_timeout=30)
    except (ComputerMissing, NotFoundException):
        if row.status == "creation_unknown":
            raise ComputerUnavailable("Private computer allocation is uncertain; reconcile it with the provider before retrying") from None
        if not create:
            raise ComputerUnavailable("This user's computer has expired. Start a new owner task to restore its saved disk.") from None
        try:
            remote = await computer.client.create(DesktopOptions(name=row.name, template=computer.config.tenki_desktop_image,
                labels=labels, timeout=computer.config.tenki_timeout), timeout=60)
        except BaseException:
            save_sandbox(host, status="creation_unknown", frame_id="")
            raise
    if any((remote.labels or {}).get(key) != value for key, value in labels.items()):
        raise ComputerUnavailable("Private computer ownership did not match; refusing to resume it")
    replaced = row.sandbox_id != remote.id
    save_sandbox(host, sandbox_id=remote.id, status="starting", error=None)
    computer.sandbox = remote
    await remote.start(timeout=60)
    save_sandbox(host, status="running", error=None)
    if replaced or row.status != "running":
        for key in related_keys(host):
            save_sandbox(key, frame_id="", **({"latest_image": ""} if replaced else {}))
    remote.select_desktop(screen)
    save_sandbox(computer.key, name="band-desktop-" + hashlib.sha256(computer.key.encode()).hexdigest()[:32],
                 sandbox_id=remote.id, status="running", error=None)
    if desktop:
        await remote.computer_use.start(request_timeout=60)
        info = await remote.computer_use.display.get_info(request_timeout=15)
        display_info = next((item for item in info.displays if item.is_active), None)
        if not display_info:
            raise ComputerUnavailable("The agent desktop did not start")
        save_sandbox(computer.key, width=display_info.width, height=display_info.height)
        computer.desktop_started = True
    return remote


async def close(computer, *, force):
    from app.computer.desktop import ComputerUnavailable, sandbox_record, save_sandbox
    host = computer.host_key
    if host_in_use(host, excluding=None if force else computer.task.id, force_key=computer.key if force else None):
        if force:
            raise ComputerUnavailable("This user's computer is still in use by a task, viewer, or takeover")
        await computer.detach()
        return
    try:
        record = sandbox_record(host)
        if record and record.sandbox_id and record.status not in {"stopped", "missing"}:
            if computer.client is None:
                computer.client = computer.client_factory()
            remote = await computer.client.get(record.sandbox_id, request_timeout=15)
            if remote.labels.get("host_key") != host or remote.labels.get("scope") != "user":
                raise ComputerUnavailable("Private computer ownership did not match")
            save_sandbox(host, status="pausing")
            await remote.stop(timeout=computer.config.tenki_lifecycle_timeout)
            save_sandbox(host, status="stopped")
            for key in related_keys(host):
                save_sandbox(key, frame_id="")
    except ComputerMissing:
        save_sandbox(host, status="missing")
    except Exception:
        save_sandbox(host, status="stop_failed", error="Pause could not be confirmed; inspect the provider")
        raise
    finally:
        await computer.detach()
