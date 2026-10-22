from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.http import mount_frontend


def test_missing_api_and_auth_routes_do_not_return_spa_html(tmp_path):
    # Backend CI deliberately does not build the frontend. Supply a small real
    # build tree so this tests the SPA fallback there as well as on developer hosts.
    directory = tmp_path / "dist"
    (directory / "assets").mkdir(parents=True)
    (directory / "index.html").write_text("<html>Workspace</html>", encoding="utf-8")
    (tmp_path / "private.txt").write_text("Do not serve this", encoding="utf-8")
    app = FastAPI()
    mount_frontend(app, directory)
    with TestClient(app) as client:
        for path in ("/api/not-a-real-endpoint", "/auth/not-a-real-callback", "/api", "/auth"):
            response = client.get(path)
            assert response.status_code == 404
            assert response.headers["content-type"].startswith("application/json")
        response = client.get("/")
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
        assert response.headers["cache-control"] == "no-cache"
        response = client.get("/%2e%2e/private.txt")
        assert response.status_code == 404 and "Do not serve this" not in response.text
