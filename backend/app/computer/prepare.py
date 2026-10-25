"""Operator-only preflight: prepare one selected computer without a model turn or chat send."""
import argparse
import asyncio
from uuid import uuid4
from sqlmodel import select

from app.computer import TaskComputer
from app.computer.providers import scope_provider
from app.computer.providers import scope_computer_key
from app.persistence.models import AgentProfile, AgentTask, Channel, ChannelMember, TaskPrincipal, db, init_db


def register_access(agent_id: int, channel_id: int):
    """Create a discoverable, completed access task; never run a model, send or touch a VM."""
    with db() as session:
        profile = session.get(AgentProfile, agent_id)
        channel = session.get(Channel, channel_id)
        if not profile or not channel or not session.get(ChannelMember, (channel_id, "agent", agent_id)):
            raise ValueError("Choose an agent that belongs to this channel")
        provider = scope_provider(session, profile, channel_id)
        key = scope_computer_key(session, profile, channel_id)
        source_key = f"operator-computer:{key}:{profile.band_agent_id}:{channel_id}"
        task = session.exec(select(AgentTask).where(AgentTask.source_key == source_key)).first()
        if task is None:
            task = AgentTask(id=uuid4().hex, source_key=source_key, agent_id=agent_id, channel_id=channel_id,
                room_id=channel.band_room_id or "", prompt="Operator computer preparation; no model or message delivery",
                status="completed", progress="Computer prepared by operator", delivery_status="sent")
            from app.workspace.access import agent_owner
            session.add_all([task, TaskPrincipal(task_id=task.id, user_id=agent_owner(session, profile))]); session.commit()
    return task, profile, channel, provider


async def prepare(agent_id: int, channel_id: int):
    with db() as session:
        if session.exec(select(AgentTask.id).where(AgentTask.agent_id == agent_id, AgentTask.channel_id == channel_id,
                AgentTask.status.in_(["queued", "running", "waiting"]))).first():
            raise ValueError("Wait for this scope's work to finish before preparing its computer")
    task, profile, channel, provider = register_access(agent_id, channel_id)
    computer = TaskComputer(task)
    try:
        await computer.screenshot()
        print(f"{provider.upper()} desktop is ready for {profile.name} in #{channel.name}.")
    finally:
        await computer.close(force=True)
    return task.id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", required=True, type=int)
    parser.add_argument("--channel", required=True, type=int)
    parser.add_argument("--register-only", action="store_true", help="Expose the selected computer without starting it")
    args = parser.parse_args()
    init_db()
    try:
        if args.register_only:
            register_access(args.agent, args.channel)
        else:
            asyncio.run(prepare(args.agent, args.channel))
    except Exception as exc:
        print("Computer preparation did not finish (" + type(exc).__name__ + "). Inspect its state before retrying.")
        raise SystemExit(1)
