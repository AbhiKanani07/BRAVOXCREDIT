"""
feedback.py
===========
Outcome logging — the calibration loop. Every time a user reports what actually
happened when they applied (applied / approved / denied), we record it against
the card and the profile numbers we saw. Over time this is the ground truth you
tune the engine's weights and PENALTY_PER_TIER against, replacing estimates with
your own data.

Same privacy stance as audit: the consumer identifier is hashed, never stored raw.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .audit import hash_token
from .storage import get_conn

OUTCOMES = {"applied", "approved", "denied"}


class FeedbackError(Exception):
    pass


@dataclass
class FeedbackRecord:
    feedback_id: str
    card_id: str
    outcome: str


class FeedbackStore:
    def __init__(self, db_path: str):
        self._db = db_path

    def record(
        self,
        consumer_token: str,
        card_id: str,
        outcome: str,
        score: Optional[int] = None,
        utilization: Optional[float] = None,
        inquiries: Optional[int] = None,
        new_accounts_24mo: Optional[int] = None,
        report_id: Optional[str] = None,
        approval_probability_shown: Optional[float] = None,
        fit_score_shown: Optional[float] = None,
        notes: Optional[str] = None,
    ) -> FeedbackRecord:
        if outcome not in OUTCOMES:
            raise FeedbackError(f"outcome must be one of {sorted(OUTCOMES)}")
        fid = str(uuid.uuid4())
        ts = datetime.now(timezone.utc).isoformat()
        with get_conn(self._db) as conn:
            conn.execute(
                "INSERT INTO feedback (feedback_id, ts, consumer_token_hash, card_id, "
                "outcome, score, utilization, inquiries, new_accounts_24mo, report_id, "
                "approval_probability_shown, fit_score_shown, notes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fid, ts, hash_token(consumer_token), card_id, outcome, score,
                 utilization, inquiries, new_accounts_24mo, report_id,
                 approval_probability_shown, fit_score_shown, notes),
            )
        return FeedbackRecord(feedback_id=fid, card_id=card_id, outcome=outcome)

    def stats(self) -> list[dict]:
        """Per-card outcome counts + realized approval rate (approved / decided)."""
        sql = (
            "SELECT card_id, "
            "SUM(CASE WHEN outcome='applied'  THEN 1 ELSE 0 END) AS applied, "
            "SUM(CASE WHEN outcome='approved' THEN 1 ELSE 0 END) AS approved, "
            "SUM(CASE WHEN outcome='denied'   THEN 1 ELSE 0 END) AS denied, "
            "COUNT(*) AS total "
            "FROM feedback GROUP BY card_id ORDER BY total DESC"
        )
        with get_conn(self._db) as conn:
            rows = conn.execute(sql).fetchall()

        out = []
        for r in rows:
            approved, denied = int(r["approved"]), int(r["denied"])
            decided = approved + denied
            out.append({
                "card_id": r["card_id"],
                "applied": int(r["applied"]),
                "approved": approved,
                "denied": denied,
                "total": int(r["total"]),
                "approval_rate": round(approved / decided, 3) if decided else None,
            })
        return out
