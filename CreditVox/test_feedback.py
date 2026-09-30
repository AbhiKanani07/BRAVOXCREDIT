"""Feedback store tests — SQLite-backed, pure stdlib."""

import pytest

from creditvox.storage import init_db
from creditvox.feedback import FeedbackStore, FeedbackError


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "fb.db")
    init_db(path)
    return path


def test_record_and_stats(db):
    fs = FeedbackStore(db)
    fs.record("session-aaa111", "discover_it_cashback", "approved", score=700,
              approval_probability_shown=89.0)
    fs.record("session-bbb222", "discover_it_cashback", "denied", score=640)
    fs.record("session-ccc333", "discover_it_cashback", "applied")
    stats = {s["card_id"]: s for s in fs.stats()}
    d = stats["discover_it_cashback"]
    assert d["approved"] == 1 and d["denied"] == 1 and d["applied"] == 1
    assert d["total"] == 3
    assert d["approval_rate"] == 0.5   # approved / (approved + denied)


def test_invalid_outcome_rejected(db):
    with pytest.raises(FeedbackError):
        FeedbackStore(db).record("session-ddd444", "amex_gold", "maybe")


def test_approval_rate_none_when_undecided(db):
    fs = FeedbackStore(db)
    fs.record("session-eee555", "amex_gold", "applied")
    d = {s["card_id"]: s for s in fs.stats()}["amex_gold"]
    assert d["approval_rate"] is None


def test_token_hashed_in_feedback(db):
    from creditvox.storage import get_conn
    from creditvox.audit import hash_token
    FeedbackStore(db).record("secret-token-xyz", "citi_double_cash", "approved")
    with get_conn(db) as c:
        row = c.execute("SELECT consumer_token_hash FROM feedback").fetchone()
    assert row["consumer_token_hash"] == hash_token("secret-token-xyz")
