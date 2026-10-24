"""App contacts and private conversations, with an optional personal Band connection."""
from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr
from sqlmodel import Session, select

from app.auth import current_user
from app.persistence.models import (Channel, ChannelInvitation, ChannelMember, ContactBlock, HumanDirectMessage,
                                    AppContact, AppContactRequest, PrivateConversation, User,
                                    db, get_session, utcnow)
from app.workspace.events import bus
from app.workspace.service import service

router = APIRouter()


class BandConnectionBody(BaseModel):
    api_key: SecretStr = Field(min_length=10, max_length=2000)


@router.get("/api/band/account")
def agent_management_account(user: User = Depends(current_user), session: Session = Depends(get_session)):
    from app.band.accounts import account_status
    from app.persistence.models import UserBandAccount
    return account_status(session.get(UserBandAccount, user.id))


@router.put("/api/band/account")
async def update_agent_management_account(body: BandConnectionBody, user: User = Depends(current_user)):
    from app.band.accounts import validate_account, save_account, account_status
    key = body.api_key.get_secret_value().strip()
    identity = await validate_account(key)
    with db() as session:
        row = save_account(session, user.id, key, identity)
        session.commit()
        result = account_status(row)
    await bus.publish({"type": "band_status", "band": await service.band_status(user.id)}, users={user.id})
    return result


@router.get("/api/band/connection")
async def platform_connection(user: User = Depends(current_user), session: Session = Depends(get_session)):
    from app.band.connections import connection_status
    from app.persistence.models import UserBandConnection
    from app.workspace.service import ServiceError
    row = session.get(UserBandConnection, user.id)
    if not row:
        return {**connection_status(None), "online": False}
    try:
        org = await service.platform_org(user.id)
        return {**connection_status(row), "online": org.online, "error": org.error}
    except ServiceError as error:
        return {**connection_status(row), "online": False, "error": error.message}


@router.put("/api/band/connection")
async def update_platform_connection(body: BandConnectionBody, user: User = Depends(current_user)):
    from app.band.connections import validate_key, save_connection, connection_status
    key = body.api_key.get_secret_value().strip()
    identity = await validate_key(key)
    with db() as session:
        row = save_connection(session, user.id, key, identity)
        session.commit()
        result = connection_status(row)
    from app.workspace.service import ServiceError
    try:
        org = await service.refresh_platform(user.id)
        return {**result, "online": org.online, "error": org.error}
    except ServiceError as error:
        return {**result, "online": False, "error": error.message}


class ContactBody(BaseModel):
    handle: str = Field(min_length=1, max_length=100, pattern=r"^@?[a-zA-Z0-9_.-]+$")
    message: str = Field(default="", max_length=500)


class DecisionBody(BaseModel):
    accept: bool


def require_app_contact(session: Session, first: int, second: int) -> None:
    if not session.get(AppContact, tuple(sorted([first, second]))):
        raise HTTPException(403, "Send a contact request and wait for acceptance first")


def is_blocked(session: Session, first: int, second: int) -> bool:
    return session.get(ContactBlock, (first, second)) is not None or session.get(ContactBlock, (second, first)) is not None


def messaging_blocked(session: Session, channel_id: int, user_id: int) -> bool:
    """Human direct messages stop when either person has blocked the other. Shared channels do not."""
    dm = session.get(HumanDirectMessage, channel_id)
    if dm is None:
        return False
    other = dm.second_user_id if dm.first_user_id == user_id else dm.first_user_id
    return is_blocked(session, user_id, other)


def _refuse_blocked(session: Session, first: int, second: int) -> None:
    if is_blocked(session, first, second):
        raise HTTPException(403, "You can't contact this person")


async def _notify(*user_ids: int) -> None:
    audience = {item for item in user_ids if item}
    if audience:
        await bus.publish({"type": "people_changed"}, users=audience)


@router.get("/api/people")
def people(user: User = Depends(current_user), session: Session = Depends(get_session)):
    contacts = session.exec(select(AppContact).where(
        (AppContact.first_user_id == user.id) | (AppContact.second_user_id == user.id))).all()
    contact_ids = {r.second_user_id if r.first_user_id == user.id else r.first_user_id for r in contacts}
    requests = session.exec(select(AppContactRequest).where(
        ((AppContactRequest.requester_id == user.id) | (AppContactRequest.recipient_id == user.id)),
        AppContactRequest.status == "pending")).all()
    users = {u.id: u for u in session.exec(select(User)).all()}

    def person(person_id: int) -> dict:
        item = users[person_id]
        return {"user_id": item.id, "name": item.display_name, "handle": item.username}

    blocked = session.exec(select(ContactBlock).where(ContactBlock.blocker_id == user.id)).all()
    sent_invites = []
    for row in session.exec(select(ChannelInvitation).where(ChannelInvitation.inviter_id == user.id,
                                                            ChannelInvitation.status == "pending")).all():
        scope = session.get(PrivateConversation, row.channel_id)
        recipient = users.get(row.recipient_id)
        if scope and recipient:
            sent_invites.append({"id": row.id, "channel_name": scope.title, "recipient_name": recipient.display_name})
    return {"contacts": [person(i) for i in sorted(contact_ids) if i in users],
            "requests": [{"id": r.id, **person(r.requester_id), "message": r.message}
                         for r in requests if r.recipient_id == user.id and r.requester_id in users],
            "sent_requests": [{"id": r.id, **person(r.recipient_id), "message": r.message}
                              for r in requests if r.requester_id == user.id and r.recipient_id in users],
            "sent_invitations": sent_invites,
            "blocked": [person(row.blocked_id) for row in blocked if row.blocked_id in users]}


@router.post("/api/people/requests")
async def request_contact(body: ContactBody, user: User = Depends(current_user)):
    async with service._dm_lock:
        with db() as session:
            target = session.exec(select(User).where(User.username == body.handle.lstrip("@").lower())).first()
            if not target:
                raise HTTPException(404, "No account has that username")
            if target.id == user.id:
                raise HTTPException(422, "Choose another person")
            _refuse_blocked(session, user.id, target.id)
            if session.get(AppContact, tuple(sorted([user.id, target.id]))):
                raise HTTPException(409, "You are already contacts")
            reverse = session.exec(select(AppContactRequest).where(AppContactRequest.requester_id == target.id,
                AppContactRequest.recipient_id == user.id, AppContactRequest.status == "pending")).first()
            if reverse:
                raise HTTPException(409, "This person already sent you a request. Accept it below.")
            row = session.exec(select(AppContactRequest).where(AppContactRequest.requester_id == user.id,
                AppContactRequest.recipient_id == target.id)).first()
            if row and row.status == "pending":
                return {"id": row.id, "status": row.status}
            row = row or AppContactRequest(id=uuid4().hex, requester_id=user.id, recipient_id=target.id)
            row.status, row.message, row.created_at = "pending", body.message.strip(), utcnow()
            session.add(row)
            session.commit()
            request_id, status, recipient_id = row.id, row.status, target.id
        await _notify(user.id, recipient_id)
        return {"id": request_id, "status": status}


@router.post("/api/people/requests/{request_id}/decision")
async def decide_contact(request_id: str, body: DecisionBody, user: User = Depends(current_user)):
    async with service._dm_lock:
        with db() as session:
            row = session.get(AppContactRequest, request_id)
            if not row or row.recipient_id != user.id:
                raise HTTPException(404, "Contact request not found")
            if row.status != "pending":
                return {"ok": True}
            row.status = "accepted" if body.accept else "declined"
            pair = tuple(sorted([row.requester_id, row.recipient_id]))
            if body.accept and not session.get(AppContact, pair):
                session.add(AppContact(first_user_id=pair[0], second_user_id=pair[1]))
            session.add(row)
            session.commit()
            requester_id = row.requester_id
        await _notify(user.id, requester_id)
    return {"ok": True}


@router.post("/api/people/requests/{request_id}/cancel")
async def cancel_contact(request_id: str, user: User = Depends(current_user)):
    async with service._dm_lock:
        with db() as session:
            row = session.get(AppContactRequest, request_id)
            if not row or row.requester_id != user.id:
                raise HTTPException(404, "Contact request not found")
            if row.status != "pending":
                return {"ok": True}
            row.status = "cancelled"
            session.add(row)
            session.commit()
            recipient_id = row.recipient_id
        await _notify(user.id, recipient_id)
    return {"ok": True}


@router.post("/api/people/{person_id}/dm")
async def human_dm(person_id: int, user: User = Depends(current_user)):
    if person_id == user.id:
        raise HTTPException(422, "Choose another person")
    pair = sorted([user.id, person_id])
    async with service._dm_lock:
        with db() as session:
            target = session.get(User, person_id)
            if not target:
                raise HTTPException(404, "Person not found")
            _refuse_blocked(session, user.id, person_id)
            require_app_contact(session, user.id, person_id)
            existing = session.exec(select(HumanDirectMessage).where(HumanDirectMessage.first_user_id == pair[0],
                HumanDirectMessage.second_user_id == pair[1])).first()
            if existing:
                return service.serialize_channel(session, session.get(Channel, existing.channel_id))
            channel = Channel(name=f"people-{uuid4().hex}", created_by=user.id)
            session.add(channel)
            session.flush()
            session.add_all([PrivateConversation(channel_id=channel.id, owner_id=user.id, kind="human_dm"),
                HumanDirectMessage(channel_id=channel.id, first_user_id=pair[0], second_user_id=pair[1]),
                *[ChannelMember(channel_id=channel.id, member_type="human", member_id=i) for i in pair]])
            session.commit()
            result = service.serialize_channel(session, channel)
        await service.publish_channel(channel.id)
        return result


class InviteBody(BaseModel):
    user_id: int


@router.post("/api/channels/{channel_id}/invitations")
async def invite_person(channel_id: int, body: InviteBody, user: User = Depends(current_user)):
    async with service._dm_lock:
        with db() as session:
            scope = session.get(PrivateConversation, channel_id)
            if not scope or scope.owner_id != user.id or scope.kind != "channel":
                raise HTTPException(403, "Only the owner of a personal channel can invite people")
            if not session.get(User, body.user_id):
                raise HTTPException(404, "Person not found")
            _refuse_blocked(session, user.id, body.user_id)
            require_app_contact(session, user.id, body.user_id)
            if session.get(ChannelMember, (channel_id, "human", body.user_id)):
                raise HTTPException(409, "This person is already in the channel")
            if session.exec(select(ChannelInvitation).where(ChannelInvitation.channel_id == channel_id,
                    ChannelInvitation.recipient_id == body.user_id, ChannelInvitation.status == "pending")).first():
                return {"ok": True}
            session.add(ChannelInvitation(id=uuid4().hex, channel_id=channel_id, inviter_id=user.id, recipient_id=body.user_id))
            session.commit()
            recipient_id = body.user_id
        await _notify(user.id, recipient_id)
    return {"ok": True}


def pending_invitations(session: Session, user_id: int) -> list[dict]:
    result = []
    for row in session.exec(select(ChannelInvitation).where(ChannelInvitation.recipient_id == user_id,
                                                           ChannelInvitation.status == "pending")).all():
        scope = session.get(PrivateConversation, row.channel_id)
        inviter = session.get(User, row.inviter_id)
        if scope and inviter:
            result.append({"id": row.id, "channel_name": scope.title, "inviter_name": inviter.display_name})
    return result


def incoming_contact_requests(session: Session, user_id: int) -> list[dict]:
    rows = session.exec(select(AppContactRequest).where(
        AppContactRequest.recipient_id == user_id, AppContactRequest.status == "pending")).all()
    requester_ids = [row.requester_id for row in rows]
    users = {item.id: item for item in session.exec(select(User).where(User.id.in_(requester_ids))).all()} if requester_ids else {}
    return [{"id": row.id, "user_id": row.requester_id, "name": users[row.requester_id].display_name,
             "handle": users[row.requester_id].username, "message": row.message}
            for row in rows if row.requester_id in users]


@router.get("/api/people/invitations")
def invitations(user: User = Depends(current_user), session: Session = Depends(get_session)):
    return pending_invitations(session, user.id)


@router.post("/api/people/invitations/{invitation_id}/decision")
async def decide_invitation(invitation_id: str, body: DecisionBody, user: User = Depends(current_user)):
    async with service._dm_lock:
        with db() as session:
            row = session.get(ChannelInvitation, invitation_id)
            if not row or row.recipient_id != user.id:
                raise HTTPException(404, "Invitation not found")
            if row.status != "pending":
                return {"ok": True}
            channel = session.get(Channel, row.channel_id)
            scope = session.get(PrivateConversation, row.channel_id)
            if not channel or not scope or scope.kind != "channel":
                raise HTTPException(404, "Channel not found")
            row.status = "accepted" if body.accept else "declined"
            if body.accept and not session.get(ChannelMember, (channel.id, "human", user.id)):
                session.add(ChannelMember(channel_id=channel.id, member_type="human", member_id=user.id))
            session.add(row)
            session.commit()
            inviter_id = row.inviter_id
        await _notify(user.id, inviter_id)
        if body.accept:
            await service.publish_channel(channel.id)
        return {"ok": True}


@router.post("/api/people/invitations/{invitation_id}/withdraw")
async def withdraw_invitation(invitation_id: str, user: User = Depends(current_user)):
    async with service._dm_lock:
        with db() as session:
            row = session.get(ChannelInvitation, invitation_id)
            if not row or row.inviter_id != user.id:
                raise HTTPException(404, "Invitation not found")
            if row.status != "pending":
                return {"ok": True}
            row.status = "withdrawn"
            session.add(row)
            session.commit()
            recipient_id = row.recipient_id
        await _notify(user.id, recipient_id)
    return {"ok": True}


@router.delete("/api/people/{person_id}")
async def remove_contact(person_id: int, user: User = Depends(current_user)):
    async with service._dm_lock:
        with db() as session:
            pair = tuple(sorted([user.id, person_id]))
            contact = session.get(AppContact, pair)
            if contact is None:
                raise HTTPException(404, "Contact not found")
            session.delete(contact)
            for row in session.exec(select(AppContactRequest).where(
                    ((AppContactRequest.requester_id == user.id) & (AppContactRequest.recipient_id == person_id))
                    | ((AppContactRequest.requester_id == person_id) & (AppContactRequest.recipient_id == user.id)),
                    AppContactRequest.status == "pending")).all():
                row.status = "cancelled"
                session.add(row)
            session.commit()
        await _notify(user.id, person_id)
    return {"ok": True}


@router.post("/api/people/{person_id}/block")
async def block_person(person_id: int, user: User = Depends(current_user)):
    if person_id == user.id:
        raise HTTPException(422, "Choose another person")
    async with service._dm_lock:
        with db() as session:
            if session.get(User, person_id) is None:
                raise HTTPException(404, "Person not found")
            if session.get(ContactBlock, (user.id, person_id)) is None:
                session.add(ContactBlock(blocker_id=user.id, blocked_id=person_id))
            for row in session.exec(select(AppContactRequest).where(
                    ((AppContactRequest.requester_id == user.id) & (AppContactRequest.recipient_id == person_id))
                    | ((AppContactRequest.requester_id == person_id) & (AppContactRequest.recipient_id == user.id)),
                    AppContactRequest.status == "pending")).all():
                row.status = "cancelled"
                session.add(row)
            session.commit()
        await _notify(user.id, person_id)
    return {"ok": True}


@router.post("/api/people/{person_id}/unblock")
async def unblock_person(person_id: int, user: User = Depends(current_user)):
    async with service._dm_lock:
        with db() as session:
            row = session.get(ContactBlock, (user.id, person_id))
            if row is not None:
                session.delete(row)
                session.commit()
        await _notify(user.id, person_id)
    return {"ok": True}
