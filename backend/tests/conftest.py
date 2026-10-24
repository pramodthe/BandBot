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


@pytest.fixture(autouse=True)
def offline_band_platform(monkeypatch):
    from fake_band_platform import OfflineBandNetwork
    import app.band.platform as transport
    import app.band.connections as connections
    import app.band.accounts as accounts
    import app.workspace.service as service_module
    network = OfflineBandNetwork()
    monkeypatch.setattr(transport, "AsyncRestClient", network.client)
    monkeypatch.setattr(connections, "AsyncRestClient", network.client)
    monkeypatch.setattr(accounts, "AsyncRestClient", network.management_client)
    monkeypatch.setattr(service_module, "PlatformListener", network.listener)
    return network


@pytest.fixture(autouse=True)
def offline_agent_keys(monkeypatch):
    from app.band.listener import AgentManager, BandIdentity
    import app.band.connections as connections
    import app.workspace.service as service_module

    async def identity(key):
        if not key.startswith("band_a_"):
            raise ValueError("Invalid test Agent API key")
        return BandIdentity(id=key.removeprefix("band_a_"), name="Remote Writer", handle="remote/writer")

    async def start(manager, profile, *, owner_id=""):
        manager._band_ids[profile.id] = profile.band_agent_id
        manager.status[profile.id] = {"state": "online", "error": None}

    monkeypatch.setattr(connections, "fetch_identity", identity)
    monkeypatch.setattr(service_module, "fetch_identity", identity)
    monkeypatch.setattr(AgentManager, "start", start)
