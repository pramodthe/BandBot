from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT_DIR / "backend"
FRONTEND_DIST = ROOT_DIR / "frontend" / "dist"

load_dotenv(ROOT_DIR / ".env")
load_dotenv(BACKEND_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    openai_api_key: str = _env("OPENAI_API_KEY")
    band_rest_url: str = _env("BAND_REST_URL", "https://app.band.ai")
    band_ws_url: str = _env("BAND_WS_URL", "wss://app.band.ai/api/v1/socket/websocket")
    app_secret: str = _env("APP_SECRET", "dev-secret-change-me")
    # production refuses the development secret. development and test keep the fallback.
    band_env: str = _env("BAND_ENV", "development")
    database_url: str = _env("DATABASE_URL", f"sqlite:///{(BACKEND_DIR / 'band_slack.db').as_posix()}")
    auth_attempt_limit: int = max(1, int(_env("AUTH_ATTEMPT_LIMIT", "20")))
    auth_attempt_window: int = max(1, int(_env("AUTH_ATTEMPT_WINDOW_SECONDS", "600")))
    # none drops mail, memory keeps it in-process for tests, smtp delivers. Memory is refused in production.
    recovery_delivery: str = _env("RECOVERY_DELIVERY", "none")
    smtp_url: str = _env("SMTP_URL")
    worker_heartbeat_stale: int = max(1, int(_env("WORKER_HEARTBEAT_STALE_SECONDS", "20")))
    worker_startup_grace: int = max(0, int(_env("WORKER_STARTUP_GRACE_SECONDS", "15")))
    default_model: str = _env("DEFAULT_MODEL", "gpt-6-luna")
    documents_path: str = _env("DOCUMENTS_PATH", str(BACKEND_DIR / "documents"))
    checkpoint_path: str = _env("AGENT_CHECKPOINT_PATH", str(BACKEND_DIR / "agent_checkpoints.db"))
    task_timeout: float = float(_env("AGENT_TASK_TIMEOUT", "900"))
    task_recursion_limit: int = int(_env("AGENT_RECURSION_LIMIT", "100"))
    worker_concurrency: int = max(1, int(_env("WORKER_CONCURRENCY", "4")))
    default_timezone: str = _env("DEFAULT_TIMEZONE", "UTC")
    mcp_config_path: str = _env("MCP_CONFIG_PATH", str(BACKEND_DIR / "mcp_servers.json"))
    max_agent_teammates: int = int(_env("MAX_AGENT_TEAMMATES", "10"))
    fast_code_execution: bool = _env("FAST_CODE_EXECUTION", "on").lower() not in ("0", "off", "false", "no")
    exa_api_key: str = _env("EXA_API_KEY")
    composio_api_key: str = _env("COMPOSIO_API_KEY")
    e2b_api_key: str = _env("E2B_API_KEY")
    e2b_template: str = _env("E2B_DESKTOP_TEMPLATE", "desktop")
    e2b_timeout: int = max(60, min(3600, int(_env("E2B_TIMEOUT_SECONDS", "1200"))))
    provider_endpoint_allowlist: str = _env("PROVIDER_ENDPOINT_ALLOWLIST")
    computer_provider: str = _env("COMPUTER_PROVIDER", "e2b").lower()
    tenki_api_key: str = _env("TENKI_API_KEY")
    tenki_desktop_image: str = _env("TENKI_DESKTOP_IMAGE")
    tenki_computer_sharing: str = _env("TENKI_COMPUTER_SHARING", "user").lower()
    tenki_timeout: int = max(60, min(3600, int(_env("TENKI_TIMEOUT_SECONDS", "1200"))))
    tenki_pause_retention: int = max(3600, int(_env("TENKI_PAUSE_RETENTION_SECONDS", "604800")))
    tenki_lifecycle_timeout: int = max(60, min(600, int(_env("TENKI_LIFECYCLE_TIMEOUT_SECONDS", "180"))))
    tenki_disk_gib: int = max(1, min(50, int(_env("TENKI_DISK_GIB", "1"))))
    app_preview_base_url: str = _env("APP_PREVIEW_BASE_URL", "http://localhost:8000")
    codex_executable: str = _env("CODEX_EXECUTABLE", "codex")
    # Exact gateway base URLs approved by the server operator; never arbitrary browser-supplied hosts.
    openclaw_gateway_urls: str = _env("OPENCLAW_GATEWAY_URLS")
    # Only enable for eligible local/open-source deployments. Hosted products need provider approval.
    codex_chatgpt_sign_in: bool = _env("CODEX_CHATGPT_SIGN_IN", "off").lower() in ("1", "on", "true")
    agent_oauth_redirect_uri: str = _env("AGENT_OAUTH_REDIRECT_URI", "http://127.0.0.1:8000/auth/callback")
    # JSON of USD per million tokens. Empty means prices are unknown, never free.
    model_prices: str = _env("MODEL_PRICES")


settings = Settings()


def is_postgres(url: str) -> bool:
    return url.split(":", 1)[0] in {"postgres", "postgresql", "postgresql+psycopg"}


def sqlalchemy_url(url: str) -> str:
    """SQLAlchemy needs the psycopg driver; the rest of the URL stays intact."""
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def postgres_conninfo(url: str) -> str:
    """psycopg accepts a libpq URL, including one copied from SQLAlchemy."""
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url.removeprefix("postgresql+psycopg://")
    if url.startswith("postgres://"):
        return "postgresql://" + url.removeprefix("postgres://")
    return url


MODELS = [
    {"id": "gpt-6-astra", "label": "GPT-6 Astra"},
    {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol"},
    {"id": "gpt-6-luna", "label": "GPT-6 Luna"},
]
