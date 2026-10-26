"""Resolve only encrypted, provider-created endpoints. Never accept a client host."""
import json
import re
from urllib.parse import urlsplit

from fastapi import HTTPException
from app.auth import decrypt_secret
from app.persistence.models import ComputerConnection, ComputerEndpoint, as_utc, db, utcnow


def endpoint(key: str, sandbox_id: str, port: int):
    with db() as session:
        if key.startswith("tenki-"):
            row = session.get(ComputerEndpoint, (key, port))
            if not row or row.sandbox_id != sandbox_id or as_utc(row.expires_at) <= utcnow():
                raise HTTPException(409, "Computer connection expired. Reconnect from the computer panel.")
            try:
                data = json.loads(decrypt_secret(row.endpoint_enc))
                parsed = urlsplit(data["origin"])
                if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                        or not re.fullmatch(r"[a-f0-9]{64}", data["secret"])):
                    raise ValueError()
                return parsed.netloc, data["secret"]
            except (ValueError, KeyError, TypeError):
                raise HTTPException(502, "Invalid computer connection") from None
        row = session.get(ComputerConnection, sandbox_id)
        if not row or not re.fullmatch(r"6080-[a-zA-Z0-9-]+\.e2b\.app", row.host):
            raise HTTPException(502, "Private computer routing is unavailable")
        return row.host.replace("6080-", f"{port}-", 1), decrypt_secret(row.access_token_enc)


def headers(key: str, secret: str, port: int):
    return {"x-band-ingress": secret, "x-band-port": str(port)} if key.startswith("tenki-") else {"e2b-traffic-access-token": secret}
