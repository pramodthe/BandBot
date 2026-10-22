"""Operator selection for future tasks. Stop the idle API and worker before changing it."""
import argparse
from sqlmodel import select
from app.computer.providers import configured
from app.persistence.models import AgentProfile, AgentTask, Channel, ChannelMember, ComputerScope, TaskComputerBinding, db, init_db


def select_provider(provider: str, *, agent_id=None, channel_id=None, all_scopes=False, preserve_waiting=False):
    if provider not in {"e2b", "tenki"} or not configured(provider):
        raise ValueError("Configure the selected provider and image before enabling it")
    if not all_scopes and (agent_id is None or channel_id is None):
        raise ValueError("Select an agent/channel, or explicitly select all scopes")
    with db() as session:
        active = select(AgentTask.id).where(AgentTask.status.in_(["queued", "running"] if preserve_waiting else ["queued", "running", "waiting"]))
        if not all_scopes:
            active = active.where(AgentTask.agent_id == agent_id, AgentTask.channel_id == channel_id)
        if session.exec(active).first():
            raise ValueError("Drain queued, running and waiting tasks before changing provider")
        if preserve_waiting:
            waiting = select(AgentTask).where(AgentTask.status == "waiting")
            if not all_scopes:
                waiting = waiting.where(AgentTask.agent_id == agent_id, AgentTask.channel_id == channel_id)
            for task in session.exec(waiting).all():
                if not session.get(TaskComputerBinding, task.id):
                    raise ValueError("Waiting tasks must have a pinned provider before selection")
        profiles = session.exec(select(AgentProfile)).all() if all_scopes else [session.get(AgentProfile, agent_id)]
        if not all(profiles):
            raise ValueError("Agent not found")
        changed = 0
        for profile in profiles:
            channels = session.exec(select(Channel)).all() if all_scopes else [session.get(Channel, channel_id)]
            for channel in channels:
                if not channel:
                    raise ValueError("Channel not found")
                # Never create cross-agent bindings for someone else's private chat.
                from app.persistence.models import DirectMessage
                dm = session.get(DirectMessage, channel.id)
                if dm and dm.agent_id != profile.id:
                    continue
                if not all_scopes or session.get(ChannelMember, (channel.id, "agent", profile.id)) or session.exec(
                        select(AgentTask.id).where(AgentTask.agent_id == profile.id, AgentTask.channel_id == channel.id)).first():
                    session.merge(ComputerScope(key=f"agent-{profile.band_agent_id}-channel-{channel.id}", provider=provider))
                    changed += 1
        session.commit()
    return changed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", required=True, choices=["e2b", "tenki"])
    parser.add_argument("--agent", type=int)
    parser.add_argument("--channel", type=int)
    parser.add_argument("--all-scopes", action="store_true")
    parser.add_argument("--preserve-waiting", action="store_true", help="Keep waiting tasks pinned to their original provider")
    args = parser.parse_args()
    init_db()
    count = select_provider(args.provider, agent_id=args.agent, channel_id=args.channel, all_scopes=args.all_scopes,
                            preserve_waiting=args.preserve_waiting)
    print(f"Selected {args.provider} for {count} scopes. Start new tasks; historical tasks retain their original computers.")
