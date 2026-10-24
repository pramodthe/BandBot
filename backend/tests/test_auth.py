from __future__ import annotations

from uuid import uuid4

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password, verify_password
from app.main import app
from app.persistence.models import User, db, init_db


@pytest.mark.parametrize("password", ["a" * 73, "a" * 128, "\u20ac" * 25, "\U0001f512" * 128])
def test_signup_and_login_preserve_the_complete_password(password):
    init_db()
    username = "auth-" + uuid4().hex[:12]
    with TestClient(app) as client:
        signed_up = client.post("/api/auth/signup", json={
            "username": username, "display_name": "Password test", "password": password,
            "band_api_key": f"band_a_signup-{username}",
        })
        assert signed_up.status_code == 200, signed_up.text
        login = client.post("/api/auth/login", json={"username": username, "password": password})
        assert login.status_code == 200, login.text
        boot = client.get("/api/bootstrap", headers={"Authorization": f"Bearer {login.json()['token']}"})
        assert boot.status_code == 200 and boot.json()["me"]["username"] == username
        # Passwords sharing their first 72 bytes must not authenticate each other.
        different = password[:-1] + "b"
        assert client.post("/api/auth/login", json={"username": username, "password": different}).status_code == 401


@pytest.mark.parametrize("password", ["secret123", "a" * 72, "\u20ac" * 24])
def test_existing_bcrypt_passwords_still_log_in(password):
    init_db()
    username = "legacy-" + uuid4().hex[:12]
    with db() as session:
        session.add(User(username=username, display_name="Legacy user",
                         password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()))
        session.commit()
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200
        assert client.post("/api/auth/login", json={"username": username, "password": "a" * 73}).status_code == 401
        assert client.post("/api/auth/login", json={"username": username, "password": "\u20ac" * 25}).status_code == 401


def test_short_new_passwords_round_trip_and_invalid_hashes_fail_closed():
    encoded = hash_password("secret123")
    assert verify_password("secret123", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("secret123", "invalid hash")
    assert not verify_password("secret123", "bcrypt-sha256$invalid hash")


def test_signup_keeps_the_128_character_limit():
    init_db()
    with TestClient(app) as client:
        username = "limit-" + uuid4().hex[:12]
        response = client.post("/api/auth/signup", json={
            "username": username, "display_name": "Password test", "password": "a" * 129,
            "band_api_key": f"band_a_signup-{username}",
        })
        assert response.status_code == 422
