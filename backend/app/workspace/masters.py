"""Create one private app coordinator per account, without network side effects."""
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.openai_provider import configuration
from app.persistence.models import AgentPersona, AgentPluginAccess, AgentProfile, BandAgentOwner, LocalTeammate, User


def ensure_user_master(session: Session, user: User) -> tuple[AgentProfile, bool]:
    # The existing unique identity constraint also protects concurrent retries.
    identity = f"local-master-{user.id}"
    existing = session.exec(select(AgentProfile).where(AgentProfile.band_agent_id == identity)).first()
    if existing:
        _check_owner(session, existing, user.id)
        return existing, False
    # Preserve a coordinator already owned by this user, including legacy native identities.
    owned = session.exec(select(AgentProfile).join(BandAgentOwner).where(
        AgentProfile.is_master == True, BandAgentOwner.user_id == user.id)).first()  # noqa: E712
    if owned:
        return owned, False
    try:
        with session.begin_nested():
            profile = AgentProfile(name="Master", role="master", is_master=True, created_by=user.id,
                model=configuration()[2], band_agent_id=identity, band_api_key_enc="",
                handle=f"app-master-{user.id}")
            session.add(profile)
            session.flush()
            session.add_all([BandAgentOwner(agent_id=profile.id, user_id=user.id, band_user_id=""),
                LocalTeammate(agent_id=profile.id, user_id=user.id),
                AgentPluginAccess(agent_id=profile.id, slugs=[]),
                AgentPersona(agent_id=profile.id, specialty="Your private team coordinator")])
            session.flush()
        return profile, True
    except IntegrityError:
        profile = session.exec(select(AgentProfile).where(AgentProfile.band_agent_id == identity)).one()
        _check_owner(session, profile, user.id)
        return profile, False


def _check_owner(session: Session, profile: AgentProfile, user_id: int) -> None:
    local = session.get(LocalTeammate, profile.id)
    owner = session.get(BandAgentOwner, profile.id)
    if not profile.is_master or not local or local.user_id != user_id or not owner or owner.user_id != user_id:
        raise RuntimeError("The saved coordinator has inconsistent ownership")
