"""Auth + history API tests. Needs requirements-dev.txt (fastapi + httpx)."""

import os
import pytest

os.environ.setdefault("DB_PATH", "test_auth.db")
os.environ.setdefault("CREDIT_PROVIDER", "bureau_mock")

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from creditvox.api import app  # noqa: E402


def _client():
    return TestClient(app)


def test_register_sets_session_and_me_works():
    c = _client()
    email = f"u{os.urandom(4).hex()}@example.com"
    r = c.post("/v1/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 200
    assert r.json()["email"] == email
    # cookie is set on the client; /me should now resolve
    assert c.get("/v1/auth/me").json()["email"] == email


def test_duplicate_registration_rejected():
    c = _client()
    email = f"u{os.urandom(4).hex()}@example.com"
    c.post("/v1/auth/register", json={"email": email, "password": "password123"})
    c2 = _client()
    r = c2.post("/v1/auth/register", json={"email": email, "password": "password123"})
    assert r.status_code == 400


def test_login_wrong_password_rejected():
    c = _client()
    email = f"u{os.urandom(4).hex()}@example.com"
    c.post("/v1/auth/register", json={"email": email, "password": "password123"})
    r = _client().post("/v1/auth/login", json={"email": email, "password": "nope"})
    assert r.status_code == 400


def test_me_requires_login():
    assert _client().get("/v1/auth/me").status_code == 401


def test_history_requires_login():
    assert _client().get("/v1/history").status_code == 401


def test_logged_in_match_is_saved_to_history():
    c = _client()
    email = f"u{os.urandom(4).hex()}@example.com"
    c.post("/v1/auth/register", json={"email": email, "password": "password123"})
    c.post("/v1/match", json={"score": 720, "utilization": 10, "inquiries": 1})
    hist = c.get("/v1/history").json()
    assert len(hist) >= 1
    assert hist[0]["score"] == 720


def test_logged_out_match_still_works():
    r = _client().post("/v1/match", json={"score": 720, "utilization": 10, "inquiries": 1})
    assert r.status_code == 200  # tool is open; no login required


def test_logout_clears_session():
    c = _client()
    email = f"u{os.urandom(4).hex()}@example.com"
    c.post("/v1/auth/register", json={"email": email, "password": "password123"})
    assert c.get("/v1/auth/me").status_code == 200
    c.post("/v1/auth/logout")
    assert c.get("/v1/auth/me").status_code == 401
