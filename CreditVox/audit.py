"""
audit.py
========
The compliance seam, now SQLite-backed (survives restarts, works across
processes).

  1. Consent  — no credit pull happens without a recorded, unexpired consent
                record tied to a permissible purpose. Mirrors FCRA: you need the
                consumer's authorization before accessing their file.
  2. Audit    — every pull is written to an append-only log for an auditable
                trail of who pulled what, when, and under which consent.

PRIVACY: we never persist a raw consumer identifier (never an SSN). The token is
hashed before it touches storage.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .storage import get_conn

# Permissible purposes we support. Pre-approval / prequalification is a SOFT pull
# and is the only one this product uses. Hard pulls are intentionally absent.
PERMISSIBLE_PURPOSES = {"prequalification"}


class ConsentError(Exception):
    """Raised when a pull is attempted without valid consent."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_token(token: str) -> str:
    """One-way hash of a consumer identifier for safe storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]


@dataclass
class ConsentRecord:
    consent_id: str
    consumer_token_hash: str
    purpose: str
    disclosure_version: str
    granted: bool
    granted_at: str          # ISO 8601 UTC
    expires_at: str          # ISO 8601 UTC
    ip: Optional[str] = None

    def is_valid(self, purpose: str) -> bool:
        if not self.granted or self.purpose != purpose:
            return False
        return _now() < datetime.fromisoformat(self.expires_at)


@dataclass
class AuditEntry:
    entry_id: str
    ts: str
    event: str
    provider: str
    inquiry_type: str
    consent_id: str
    consumer_token_hash: str
    report_id: Optional[str] = None
    detail: dict = field(default_factory=dict)


class ConsentStore:
    """SQLite-backed consent registry."""

    def __init__(self, db_path: str, ttl_minutes: int, disclosure_version: str):
        self._db = db_path
        self._ttl = timedelta(minutes=ttl_minutes)
        self._disclosure_version = disclosure_version

    def grant(self, consumer_token: str, purpose: str,
              ip: Optional[str] = None) -> ConsentRecord:
        if purpose not in PERMISSIBLE_PURPOSES:
            raise ConsentError(f"Unsupported permissible purpose: {purpose!r}")
        now = _now()
        rec = ConsentRecord(
            consent_id=str(uuid.uuid4()),
            consumer_token_hash=hash_token(consumer_token),
            purpose=purpose,
            disclosure_version=self._disclosure_version,
            granted=True,
            granted_at=now.isoformat(),
            expires_at=(now + self._ttl).isoformat(),
            ip=ip,
        )
        with get_conn(self._db) as conn:
            conn.execute(
                "INSERT INTO consents (consent_id, consumer_token_hash, purpose, "
                "disclosure_version, granted, granted_at, expires_at, ip) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (rec.consent_id, rec.consumer_token_hash, rec.purpose,
                 rec.disclosure_version, int(rec.granted), rec.granted_at,
                 rec.expires_at, rec.ip),
            )
        return rec

    def get(self, consent_id: str) -> Optional[ConsentRecord]:
        with get_conn(self._db) as conn:
            row = conn.execute(
                "SELECT * FROM consents WHERE consent_id = ?", (consent_id,)
            ).fetchone()
        if row is None:
            return None
        return ConsentRecord(
            consent_id=row["consent_id"],
            consumer_token_hash=row["consumer_token_hash"],
            purpose=row["purpose"],
            disclosure_version=row["disclosure_version"],
            granted=bool(row["granted"]),
            granted_at=row["granted_at"],
            expires_at=row["expires_at"],
            ip=row["ip"],
        )

    def require(self, consent_id: str, purpose: str) -> ConsentRecord:
        rec = self.get(consent_id)
        if rec is None:
            raise ConsentError("No consent on file for this request.")
        if not rec.is_valid(purpose):
            raise ConsentError("Consent is missing, expired, or purpose mismatch.")
        return rec


class AuditLogger:
    """SQLite-backed append-only audit trail."""

    def __init__(self, db_path: str):
        self._db = db_path

    def log(self, event: str, provider: str, inquiry_type: str,
            consent: ConsentRecord, report_id: Optional[str] = None,
            detail: Optional[dict] = None) -> AuditEntry:
        entry = AuditEntry(
            entry_id=str(uuid.uuid4()),
            ts=_now().isoformat(),
            event=event,
            provider=provider,
            inquiry_type=inquiry_type,
            consent_id=consent.consent_id,
            consumer_token_hash=consent.consumer_token_hash,
            report_id=report_id,
            detail=detail or {},
        )
        with get_conn(self._db) as conn:
            conn.execute(
                "INSERT INTO audit_log (entry_id, ts, event, provider, "
                "inquiry_type, consent_id, consumer_token_hash, report_id, detail) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (entry.entry_id, entry.ts, entry.event, entry.provider,
                 entry.inquiry_type, entry.consent_id, entry.consumer_token_hash,
                 entry.report_id, json.dumps(entry.detail)),
            )
        return entry
