"""
API integration tests. Requires the web extras: pip install -r requirements-dev.txt
(fastapi + httpx for TestClient).
"""

import os
import pytest

# Isolate the test DB before importing the app (config reads env at import).
os.environ.setdefault("DB_PATH", "test_api.db")
os.environ.setdefault("CREDIT_PROVIDER", "bureau_mock")

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402
from creditvox.api import app  # noqa: E402

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_ready_reports_provider():
    body = client.get("/ready").json()
    assert body["status"] == "ready"
    assert body["cards_loaded"] >= 5


def test_cards_catalog():
    cards = client.get("/v1/cards").json()
    assert len(cards) >= 5
    assert {"id", "name", "min_tier", "max_utilization"} <= cards[0].keys()


def test_direct_match():
    r = client.post("/v1/match?rank_by=fit&include_ineligible=true",
                    json={"score": 710, "utilization": 15, "inquiries": 1})
    assert r.status_code == 200
    data = r.json()
    assert any(m["eligible"] for m in data)


def test_match_rejects_out_of_range_score():
    r = client.post("/v1/match", json={"score": 999, "utilization": 10, "inquiries": 0})
    assert r.status_code == 422


def test_match_from_report_requires_consent():
    r = client.post("/v1/match-from-report",
                    json={"consent_id": "bogus", "consumer_token": "session-zzz999"})
    assert r.status_code == 403
    assert r.json()["error"] == "consent_required"


def test_full_consent_and_pull_flow():
    consent = client.post("/v1/consent",
                          json={"consumer_token": "session-flow01", "agreed": True}).json()
    assert "consent_id" in consent
    out = client.post("/v1/match-from-report", json={
        "consent_id": consent["consent_id"],
        "consumer_token": "session-flow01",
        "rank_by": "approval",
        "include_ineligible": False,
    }).json()
    assert out["report"]["inquiry_type"] == "soft"
    assert isinstance(out["matches"], list)


def test_consent_requires_agreement():
    r = client.post("/v1/consent", json={"consumer_token": "session-noagree", "agreed": False})
    assert r.status_code == 403


def test_feedback_roundtrip_and_stats():
    r = client.post("/v1/feedback", json={
        "consumer_token": "session-fb001",
        "card_id": "discover_it_cashback",
        "outcome": "approved",
        "score": 700,
        "approval_probability_shown": 88.0,
    })
    assert r.status_code == 200 and r.json()["recorded"] is True
    stats = client.get("/v1/feedback/stats").json()
    assert any(s["card_id"] == "discover_it_cashback" for s in stats)


def test_feedback_rejects_unknown_card():
    r = client.post("/v1/feedback", json={
        "consumer_token": "session-fb002",
        "card_id": "not_a_real_card",
        "outcome": "approved",
    })
    assert r.status_code == 422
