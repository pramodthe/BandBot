import os
import tempfile
from pathlib import Path

_db = Path(tempfile.mkdtemp()) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_db.as_posix()}"
os.environ["DOCUMENTS_PATH"] = str(_db.parent / "documents")
os.environ["MASTER_AGENT_NAME"] = "Master"
for key in ("BAND_USER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "E2B_API_KEY", "EXA_API_KEY", "COMPOSIO_API_KEY", "MODEL_PRICES"):
    os.environ[key] = ""
