import asyncio
from pathlib import Path

import pytest

from app.auth import encrypt_secret
from app.persistence.models import (BandRoomAccount, Channel, Message, UserBandAccount,
                                    UserBandConnection, db)
from app.workspace.service import ServiceError
from helpers import make_alice
def test_agent_transport_does_not_decrypt_management_credentials(svc):
    user = make_alice()
    with db() as session:
        session.add(UserBandAccount(user_id=user.id, band_user_id="old-human", secret_enc="cannot-decrypt"))
        session.add(UserBandConnection(user_id=user.id, band_agent_id="alice",
                                       secret_enc=encrypt_secret("band_a_alice")))
        session.commit()
    status = asyncio.run(svc.band_status(user.id))
    assert status["api"] == "agent" and status["online"] is True
    channel = asyncio.run(svc.create_channel("Agent channel", "", user))
    assert asyncio.run(svc.room_org(channel["id"])).identity == "alice"


def test_old_room_keeps_history_and_cannot_select_a_different_transport(svc):
    user = make_alice()
    with db() as session:
        session.add(UserBandConnection(user_id=user.id, band_agent_id="alice",
                                       secret_enc=encrypt_secret("band_a_alice")))
        channel = Channel(name="older-room", band_room_id="old-native-room", created_by=user.id)
        session.add(channel)
        session.flush()
        session.add(BandRoomAccount(channel_id=channel.id, band_user_id="old-human", user_id=user.id))
        message = Message(channel_id=channel.id, author_type="human", author_id=user.id,
                          author_name="Alice", content="Keep this history")
        session.add(message)
        session.commit()
    with pytest.raises(ServiceError, match="older channel"):
        asyncio.run(svc.room_org(channel.id))
    with db() as session:
        assert session.get(Message, message.id).content == "Keep this history"
        assert session.get(Channel, channel.id).band_room_id == "old-native-room"


def test_account_apis_are_limited_to_agent_management_and_never_use_workspace_key():
    root = Path(__file__).resolve().parents[1] / "app"
    # Prevent a new fallback/client from silently reopening the removed integration.
    for source in root.rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        if source.name != "accounts.py":
            assert ".human_api_" not in text, source
        else:
            assert ".human_api_chats" not in text
            assert ".human_api_messages" not in text
            assert ".human_api_participants" not in text
        assert '"BAND_USER_API_KEY"' not in text, source
