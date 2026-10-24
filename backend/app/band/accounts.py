"""Per-user Band credentials for native agent management, separate from local chat."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

from band.client.rest import AsyncRestClient, aclose_rest_client
from band_rest.types.agent_register_request import AgentRegisterRequest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.auth import decrypt_secret, encrypt_secret
from app.band.platform import NO_RETRY
from app.config import settings
from app.persistence.models import UserBandAccount, db


@dataclass(frozen=True)
class BandAccountIdentity:
    id: str
    name: str
    handle: str


async def validate_account(key: str) -> BandAccountIdentity:
    if not key.strip().startswith("band_u_"):
        raise HTTPException(422, "Enter your Band user API key beginning with band_u_ to create and manage agents. Existing agent keys are connected separately.")
    client = AsyncRestClient(api_key=key.strip(), base_url=settings.band_rest_url)
    try:
        async with asyncio.timeout(25):
            profile = (await client.human_api_profile.get_my_profile(request_options=NO_RETRY)).data
        name = " ".join(part for part in (profile.first_name, profile.last_name) if part) or profile.handle
        return BandAccountIdentity(profile.id, name, profile.handle)
    except Exception:
        raise HTTPException(422, "Band could not verify this user key. Check the complete key and your account's agent-management access, then retry.") from None
    finally:
        await aclose_rest_client(client)


def save_account(session: Session, user_id: int, key: str, identity: BandAccountIdentity) -> UserBandAccount:
    row = session.get(UserBandAccount, user_id)
    if row and row.band_user_id and row.band_user_id != identity.id:
        raise HTTPException(409, "Reconnect the same Band account. Your agents belong to that account.")
    duplicate = session.exec(select(UserBandAccount).where(UserBandAccount.band_user_id == identity.id,
                                                          UserBandAccount.user_id != user_id)).first()
    if duplicate:
        raise HTTPException(409, "This Band account is already connected to another app user. Use your own key.")
    row = row or UserBandAccount(user_id=user_id)
    row.band_user_id, row.name, row.handle = identity.id, identity.name, identity.handle
    row.secret_enc = encrypt_secret(key.strip())
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "This Band account is already connected to another app user. Use your own key.") from None
    return row


def account_status(row: UserBandAccount | None) -> dict:
    return {"connected": bool(row and row.secret_enc), "name": row.name if row else "",
            "handle": row.handle if row else "", "can_manage_agents": bool(row and row.secret_enc)}


@asynccontextmanager
async def agent_management(user_id: int, *, owner_identity: str | None = None):
    """Never use a workspace environment key or another user's credential."""
    with db() as session:
        row = session.get(UserBandAccount, user_id)
    if not row or not row.secret_enc:
        raise HTTPException(409, "Add your Band user API key in People to create and manage Band agents.")
    if owner_identity and row.band_user_id != owner_identity:
        raise HTTPException(409, "Reconnect the Band account that owns this agent.")
    try:
        key = decrypt_secret(row.secret_enc)
    except Exception:
        raise HTTPException(409, "Update your Band user API key in People.") from None
    if not key.startswith("band_u_"):
        raise HTTPException(409, "Update your Band user API key in People.")
    client = AsyncRestClient(api_key=key, base_url=settings.band_rest_url)
    try:
        yield client, row.band_user_id
    finally:
        await aclose_rest_client(client)


async def register_agent(user_id: int, name: str, description: str) -> tuple[str, str, str]:
    async with agent_management(user_id) as (client, owner):
        try:
            async with asyncio.timeout(35):
                response = await client.human_api_agents.register_my_agent(
                    agent=AgentRegisterRequest(name=name, description=description), request_options=NO_RETRY)
        except Exception:
            raise HTTPException(502, "Band could not confirm agent creation. Check your agents in Band before retrying; the request may have succeeded.") from None
        return response.data.agent.id, response.data.credentials.api_key, owner


async def delete_agent(user_id: int, agent_id: str, owner_identity: str) -> None:
    async with agent_management(user_id, owner_identity=owner_identity) as (client, _):
        try:
            async with asyncio.timeout(35):
                await client.human_api_agents.delete_my_agent(agent_id, force=True, request_options=NO_RETRY)
        except Exception as error:
            if getattr(error, "status_code", None) != 404:
                raise HTTPException(502, "Band could not confirm agent removal. Check the agent in Band before retrying.") from None
