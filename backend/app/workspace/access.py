"""Application ownership and membership, independent of browser controls."""
from sqlmodel import Session, select

from app.persistence.models import (AgentProfile, BandAgentOwner, Channel, ChannelMember, DirectMessage,
                                    HumanDirectMessage, PrivateConversation, db)


def _human_member(session: Session, channel_id: int, user_id: int) -> bool:
    return session.get(ChannelMember, (channel_id, "human", user_id)) is not None


def _members(session: Session, channel_id: int) -> set[int]:
    return set(session.exec(select(ChannelMember.member_id).where(
        ChannelMember.channel_id == channel_id, ChannelMember.member_type == "human")).all())


def channel_visible(session: Session, channel_id: int, user_id: int) -> bool:
    channel = session.get(Channel, channel_id)
    if not channel:
        return False
    dm = session.get(DirectMessage, channel_id)
    if dm:
        return dm.user_id == user_id
    if session.get(PrivateConversation, channel_id) or session.get(HumanDirectMessage, channel_id):
        return _human_member(session, channel_id, user_id)
    # #general is the workspace Band room. Other logins do not inherit it.
    if channel.name == "general":
        return _human_member(session, channel_id, user_id)
    return True


def channel_audience(channel_id: int) -> set[int] | None:
    with db() as session:
        channel = session.get(Channel, channel_id)
        if not channel:
            return set()
        dm = session.get(DirectMessage, channel_id)
        if dm:
            return {dm.user_id}
        if (session.get(PrivateConversation, channel_id) or session.get(HumanDirectMessage, channel_id)
                or channel.name == "general"):
            return _members(session, channel_id)
        return None


def agent_owner(session: Session, profile: AgentProfile) -> int | None:
    owner = session.get(BandAgentOwner, profile.id)
    return owner.user_id if owner else None


def agent_visible(session: Session, profile: AgentProfile, user_id: int) -> bool:
    owner = agent_owner(session, profile)
    if owner is None or owner == user_id:
        return True
    channels = session.exec(select(ChannelMember.channel_id).where(
        ChannelMember.member_type == "agent", ChannelMember.member_id == profile.id)).all()
    return any(channel_visible(session, cid, user_id) for cid in channels)


def agent_event_visible(user_id: int, agent_id: int) -> bool:
    with db() as session:
        profile = session.get(AgentProfile, agent_id)
        return profile is None or agent_visible(session, profile, user_id)


def channel_teammates(session: Session, channel_id: int) -> list[AgentProfile]:
    members = set(session.exec(select(ChannelMember.member_id).where(
        ChannelMember.channel_id == channel_id, ChannelMember.member_type == "agent")).all())
    private = session.get(PrivateConversation, channel_id)
    return [a for a in session.exec(select(AgentProfile)).all()
            if a.id in members or (not private and agent_owner(session, a) is None)]
