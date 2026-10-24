"""Validate and store personal Band connections without changing their ownership."""
from contextlib import asynccontextmanager

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.auth import encrypt_secret
from app.band.org import BandOrg
from app.persistence.models import UserBandAccount


@asynccontextmanager
async def authenticated_account(key: str):
    key = key.strip()
    if not key.startswith("band_u_"):
        raise HTTPException(422, "Use a Band human API key (band_u_…) from Band settings, not an agent or model-provider key.")
    org = BandOrg(key)
    try:
        await org.connect()
        if not org.online or not org.owner:
            raise HTTPException(422, "Band authentication failed. Check your human API key and Human API access, then retry.")
        yield org
    finally:
        await org.close()


def save_account(session: Session, user_id: int, key: str, org: BandOrg) -> UserBandAccount:
    """Flush in the caller's transaction so signup and linking succeed together."""
    row = session.get(UserBandAccount, user_id) or UserBandAccount(user_id=user_id)
    if row.band_user_id and row.band_user_id != org.owner.id:
        raise HTTPException(409, "This account is linked to another Band identity. Reconnect the same Band account.")
    duplicate = session.exec(select(UserBandAccount).where(UserBandAccount.band_user_id == org.owner.id,
                                                          UserBandAccount.user_id != user_id)).first()
    if duplicate:
        raise HTTPException(409, "This Band account is already connected to another app user")
    row.band_user_id, row.handle, row.name = org.owner.id, org.owner.handle, org.owner.name
    row.secret_enc = encrypt_secret(key.strip())
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "This Band account is already connected to another app user") from None
    return row
