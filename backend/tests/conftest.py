import os
import tempfile
from pathlib import Path
from uuid import uuid4

_db = Path(tempfile.mkdtemp()) / "test.db"
if os.environ.get("BAND_USE_ENV_DATABASE") != "1":
    os.environ["DATABASE_URL"] = f"sqlite:///{_db.as_posix()}"
os.environ["DOCUMENTS_PATH"] = str(_db.parent / "documents")
os.environ["AUTH_ATTEMPT_LIMIT"] = "100000"
os.environ["RECOVERY_DELIVERY"] = "memory"
os.environ["BAND_ENV"] = "test"
os.environ["OPENCLAW_GATEWAY_URLS"] = ""
for key in ("BAND_USER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CURSOR_API_KEY", "E2B_API_KEY", "EXA_API_KEY", "COMPOSIO_API_KEY", "MODEL_PRICES", "GOOGLE_API_KEY", "GEMINI_API_KEY", "TAVILY_API_KEY", "PROVIDER_ENDPOINT_ALLOWLIST", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL"):
    os.environ[key] = ""
for key in ("TENKI_API_KEY", "TENKI_AUTH_TOKEN", "TENKI_DESKTOP_IMAGE"):
    os.environ[key] = ""
os.environ["COMPUTER_PROVIDER"] = "e2b"
os.environ["TENKI_COMPUTER_SHARING"] = "agent_channel"

import pytest


class _OfflineRest:
    """Stand-in for a Band REST client. Tests must not open a network connection."""


def _offline_personal(org) -> bool:
    return isinstance(getattr(org, "_rest", None), _OfflineRest)


@pytest.fixture(autouse=True)
def offline_personal_band_keys(monkeypatch):
    """Signup verifies a Band human key. Tests accept band_u_ keys without calling Band."""
    from app.auth import decrypt_secret
    from app.band.listener import AgentManager, BandIdentity
    from app.band.org import BandOrg, Owner
    from app.config import settings
    import app.workspace.service as service_module

    original_connect = BandOrg.connect
    original_close = BandOrg.close
    original_register = BandOrg.register_agent
    original_create_room = BandOrg.create_room
    original_add = BandOrg.add_participant
    original_remove = BandOrg.remove_participant
    original_send = BandOrg.send
    original_messages = BandOrg.messages_since
    original_delete = BandOrg.delete_agent
    original_peers = BandOrg.agent_peers
    original_identity = service_module.fetch_identity
    original_start = AgentManager.start

    async def connect(self):
        if self._api_key is None:
            return await original_connect(self)
        key = self._api_key.strip()
        if not key.startswith("band_u_"):
            self.error = "Use a Band human API key (band_u_...) from your Band settings."
            self.owner = None
            self._rest = None
            return
        identity = key.removeprefix("band_u_")
        self.owner = Owner(id=identity, name=identity, handle=identity)
        self._rest = _OfflineRest()
        self.error = None

    async def close(self):
        if _offline_personal(self):
            self._rest = None
            return
        await original_close(self)

    async def register_agent(self, name, description):
        if settings.band_env == "test" and self.owner and (self._api_key or "").startswith("band_u_"):
            return f"{self.owner.id}/{name}", f"key-{self.owner.id}:{name}"
        return await original_register(self, name, description)

    async def create_room(self, title):
        if _offline_personal(self):
            room = uuid4().hex
            self._room_members[room] = {self.owner.id} if self.owner else set()
            return room
        return await original_create_room(self, title)

    async def add_participant(self, room_id, band_id):
        if _offline_personal(self):
            self._room_members.setdefault(room_id, set()).add(band_id)
            return
        await original_add(self, room_id, band_id)

    async def remove_participant(self, room_id, band_id):
        if _offline_personal(self):
            self._room_members.get(room_id, set()).discard(band_id)
            return
        await original_remove(self, room_id, band_id)

    async def send(self, room_id, content, mentions):
        if _offline_personal(self):
            return uuid4().hex
        return await original_send(self, room_id, content, mentions)

    async def messages_since(self, room_id, since):
        if _offline_personal(self):
            return []
        return await original_messages(self, room_id, since)

    async def delete_agent(self, agent_id):
        if _offline_personal(self):
            return
        await original_delete(self, agent_id)

    async def agent_peers(self, room_id):
        if _offline_personal(self):
            return []
        return await original_peers(self, room_id)

    async def fetch_identity(api_key):
        if isinstance(api_key, str) and api_key.startswith("key-") and ":" in api_key:
            owner, name = api_key.removeprefix("key-").split(":", 1)
            return BandIdentity(id=f"{owner}/{name}", name=name, handle=f"{owner}/{name.lower()}")
        return await original_identity(api_key)

    async def start(self, profile, *, owner_id=""):
        secret = decrypt_secret(profile.band_api_key_enc)
        if isinstance(secret, str) and secret.startswith("key-") and ":" in secret:
            assert profile.id is not None
            self._band_ids[profile.id] = profile.band_agent_id
            self.status[profile.id] = {"state": "online", "error": None}
            return
        await original_start(self, profile, owner_id=owner_id)

    monkeypatch.setattr(BandOrg, "connect", connect)
    monkeypatch.setattr(BandOrg, "close", close)
    monkeypatch.setattr(BandOrg, "register_agent", register_agent)
    monkeypatch.setattr(BandOrg, "create_room", create_room)
    monkeypatch.setattr(BandOrg, "add_participant", add_participant)
    monkeypatch.setattr(BandOrg, "remove_participant", remove_participant)
    monkeypatch.setattr(BandOrg, "send", send)
    monkeypatch.setattr(BandOrg, "messages_since", messages_since)
    monkeypatch.setattr(BandOrg, "delete_agent", delete_agent)
    monkeypatch.setattr(BandOrg, "agent_peers", agent_peers)
    monkeypatch.setattr(service_module, "fetch_identity", fetch_identity)
    monkeypatch.setattr(AgentManager, "start", start)
