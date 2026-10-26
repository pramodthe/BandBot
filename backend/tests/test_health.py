from fastapi.testclient import TestClient

from app.main import app


def test_health_is_ok_without_a_workspace_band_key(monkeypatch):
    # An old server setting cannot reactivate the removed Human API transport.
    monkeypatch.setenv("BAND_USER_API_KEY", "band_u_ignored")
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert "band_u_ignored" not in response.text
