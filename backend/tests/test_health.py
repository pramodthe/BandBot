from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.workspace.service import service


def test_health_is_ok_when_band_is_not_configured():
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["band"] == {"configured": False, "online": False}


def test_health_fails_when_band_setup_fails():
    previous = settings.band_user_api_key
    previous_error = service.org.error
    try:
        with TestClient(app) as client:
            # Set after startup so this does not call Band during application boot.
            object.__setattr__(settings, "band_user_api_key", "band_u_test")
            service.org.error = "Band couldn't register the agent: Validation failed (HTTP 422)"
            response = client.get("/api/health")
        assert response.status_code == 503
        body = response.json()
        assert body["ok"] is False
        assert body["band"] == {"configured": True, "online": False}
        assert "error" not in body["band"]
        assert "Validation failed" not in response.text
    finally:
        object.__setattr__(settings, "band_user_api_key", previous)
        service.org.error = previous_error
