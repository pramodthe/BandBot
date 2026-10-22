import os
import tempfile
from pathlib import Path

_db = Path(tempfile.mkdtemp()) / "test.db"
if os.environ.get("BAND_USE_ENV_DATABASE") != "1":
    os.environ["DATABASE_URL"] = f"sqlite:///{_db.as_posix()}"
os.environ["DOCUMENTS_PATH"] = str(_db.parent / "documents")
os.environ["AUTH_ATTEMPT_LIMIT"] = "100000"
os.environ["RECOVERY_DELIVERY"] = "memory"
os.environ["BAND_ENV"] = "test"
os.environ["OPENCLAW_GATEWAY_URLS"] = ""
for key in ("BAND_USER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CURSOR_API_KEY", "E2B_API_KEY", "EXA_API_KEY", "COMPOSIO_API_KEY", "MODEL_PRICES"):
    os.environ[key] = ""
for key in ("TENKI_API_KEY", "TENKI_AUTH_TOKEN", "TENKI_DESKTOP_IMAGE"):
    os.environ[key] = ""
os.environ["COMPUTER_PROVIDER"] = "e2b"
os.environ["TENKI_COMPUTER_SHARING"] = "agent_channel"
