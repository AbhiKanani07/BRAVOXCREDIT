"""Consent + audit + provider tests — SQLite-backed, pure stdlib."""

import pytest

from creditvox.storage import init_db, get_conn
from creditvox.audit import ConsentStore, AuditLogger, ConsentError, hash_token
from creditvox.providers import get_provider, ApplicantRef


@pytest.fixture()
def db(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


def test_consent_persists_across_instances(db):
    rec = ConsentStore(db, 30, "v1").grant("session-aaa111", "prequalification")
    fresh = ConsentStore(db, 30, "v1")
    assert fresh.require(rec.consent_id, "prequalification").consent_id == rec.consent_id


def test_unknown_consent_rejected(db):
    with pytest.raises(ConsentError):
        ConsentStore(db, 30, "v1").require("nope", "prequalification")


def test_expired_consent_rejected(db):
    rec = ConsentStore(db, 0, "v1").grant("session-bbb222", "prequalification")  # ttl 0 => already expired
    with pytest.raises(ConsentError):
        ConsentStore(db, 0, "v1").require(rec.consent_id, "prequalification")


def test_unsupported_purpose_rejected(db):
    with pytest.raises(ConsentError):
        ConsentStore(db, 30, "v1").grant("session-ccc333", "marketing")


def test_token_never_stored_raw(db):
    store = ConsentStore(db, 30, "v1")
    consent = store.grant("super-secret-token", "prequalification")
    AuditLogger(db).log("credit_pull", "mock_bureau", "soft", consent, "rpt-1")
    with get_conn(db) as c:
        rows = c.execute("SELECT consumer_token_hash FROM audit_log").fetchall()
    assert rows
    assert all(r["consumer_token_hash"] == hash_token("super-secret-token") for r in rows)
    assert all("super-secret-token" not in r["consumer_token_hash"] for r in rows)


def test_provider_refuses_without_consent(db):
    prov = get_provider("bureau_mock")

    class Denied:
        def is_valid(self, purpose):
            return False

    with pytest.raises(ConsentError):
        prov.fetch(ApplicantRef(token="t"), Denied())


def test_mock_provider_is_deterministic(db):
    consent = ConsentStore(db, 30, "v1").grant("session-ddd444", "prequalification")
    prov = get_provider("bureau_mock")
    r1 = prov.fetch(ApplicantRef(token="stable-token"), consent)
    r2 = prov.fetch(ApplicantRef(token="stable-token"), consent)
    assert (r1.score, r1.utilization, r1.inquiries) == (r2.score, r2.utilization, r2.inquiries)


def test_real_provider_refuses_until_wired(db):
    consent = ConsentStore(db, 30, "v1").grant("session-eee555", "prequalification")
    with pytest.raises(NotImplementedError):
        get_provider("bureau").fetch(ApplicantRef(token="t"), consent)
