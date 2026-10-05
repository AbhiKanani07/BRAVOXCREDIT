"""
auth.py
=======
Email + password accounts, sessions, and saved check history. Pure stdlib.

Security choices:
  * Passwords hashed with PBKDF2-HMAC-SHA256, 600k rounds, per-user random salt.
    (stdlib hashlib — no bcrypt/argon2 dependency to install.)
  * Session tokens are random 32-byte URL-safe strings. We store only their
    SHA-256 hash, so a database leak never exposes a usable session token.
  * All comparisons that matter are constant-time (hmac.compare_digest).

Login is OPTIONAL: the matching tool works without an account; signing in just
lets us persist a user's check history.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from .storage import get_conn

PBKDF2_ROUNDS = 600_000
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    """Registration/login failure (bad input, duplicate email, bad credentials)."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- password hashing -------------------------------------------------------
def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt), PBKDF2_ROUNDS)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _validate(email: str, password: str) -> str:
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        raise AuthError("Enter a valid email address.")
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters.")
    return email


class AuthStore:
    def __init__(self, db_path: str, session_ttl_days: int = 30):
        self._db = db_path
        self._ttl = timedelta(days=session_ttl_days)

    # --- accounts ----------------------------------------------------------
    def register(self, email: str, password: str) -> dict:
        email = _validate(email, password)
        with get_conn(self._db) as conn:
            existing = conn.execute(
                "SELECT 1 FROM users WHERE email = ?", (email,)
            ).fetchone()
            if existing:
                raise AuthError("An account with that email already exists.")
            user_id = str(uuid.uuid4())
            unsub = secrets.token_urlsafe(24)
            conn.execute(
                "INSERT INTO users (user_id, email, password_hash, created_at, "
                "email_opt_in, unsubscribe_token) VALUES (?,?,?,?,1,?)",
                (user_id, email, hash_password(password), _now().isoformat(), unsub),
            )
        return {"user_id": user_id, "email": email}

    def authenticate(self, email: str, password: str) -> dict:
        email = email.strip().lower()
        with get_conn(self._db) as conn:
            row = conn.execute(
                "SELECT user_id, email, password_hash FROM users WHERE email = ?",
                (email,),
            ).fetchone()
        # Always run a verify to keep timing uniform whether or not the user exists.
        stored = row["password_hash"] if row else hash_password("decoy")
        if not verify_password(password, stored) or row is None:
            raise AuthError("Incorrect email or password.")
        return {"user_id": row["user_id"], "email": row["email"]}

    # --- sessions ----------------------------------------------------------
    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        with get_conn(self._db) as conn:
            conn.execute(
                "INSERT INTO sessions (session_hash, user_id, created_at, expires_at) "
                "VALUES (?,?,?,?)",
                (_hash_token(token), user_id, now.isoformat(),
                 (now + self._ttl).isoformat()),
            )
        return token  # raw token goes in the cookie; only its hash is stored

    def user_for_session(self, token: Optional[str]) -> Optional[dict]:
        if not token:
            return None
        with get_conn(self._db) as conn:
            row = conn.execute(
                "SELECT s.expires_at, u.user_id, u.email "
                "FROM sessions s JOIN users u ON u.user_id = s.user_id "
                "WHERE s.session_hash = ?",
                (_hash_token(token),),
            ).fetchone()
        if row is None:
            return None
        if _now() >= datetime.fromisoformat(row["expires_at"]):
            self.delete_session(token)
            return None
        return {"user_id": row["user_id"], "email": row["email"]}

    def delete_session(self, token: Optional[str]) -> None:
        if not token:
            return
        with get_conn(self._db) as conn:
            conn.execute("DELETE FROM sessions WHERE session_hash = ?",
                         (_hash_token(token),))

    # --- saved history -----------------------------------------------------
    def save_check(self, user_id: str, profile: dict, rank_by: str,
                   source: str, matches: list[dict]) -> None:
        eligible = [m for m in matches if m.get("eligible")]
        top = eligible[0] if eligible else None
        with get_conn(self._db) as conn:
            conn.execute(
                "INSERT INTO saved_checks (check_id, user_id, created_at, score, "
                "utilization, inquiries, new_accounts_24mo, rank_by, source, "
                "top_card_id, top_card_name, top_probability, result_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), user_id, _now().isoformat(),
                 profile.get("score"), profile.get("utilization"),
                 profile.get("inquiries"), profile.get("new_accounts_24mo"),
                 rank_by, source,
                 top["card_id"] if top else None,
                 top["card_name"] if top else None,
                 top["approval_probability"] if top else None,
                 len(eligible)),
            )

    # --- email opt-out -----------------------------------------------------
    def unsubscribe(self, token: str) -> bool:
        """One-click unsubscribe via the token embedded in emails. Returns True
        if a matching user was found and opted out."""
        if not token:
            return False
        with get_conn(self._db) as conn:
            row = conn.execute(
                "SELECT user_id FROM users WHERE unsubscribe_token = ?", (token,)
            ).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE users SET email_opt_in = 0 WHERE user_id = ?",
                         (row["user_id"],))
        return True

    def history(self, user_id: str, limit: int = 50) -> list[dict]:
        with get_conn(self._db) as conn:
            rows = conn.execute(
                "SELECT created_at, score, utilization, inquiries, rank_by, source, "
                "top_card_name, top_probability, result_count "
                "FROM saved_checks WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_check(self, user_id: str) -> Optional[dict]:
        """Most recent check's full profile (incl. new_accounts) for nudges."""
        with get_conn(self._db) as conn:
            row = conn.execute(
                "SELECT score, utilization, inquiries, new_accounts_24mo "
                "FROM saved_checks WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def trend(self, user_id: str, limit: int = 30) -> list[dict]:
        """Chronological score/utilization points for the dashboard chart."""
        with get_conn(self._db) as conn:
            rows = conn.execute(
                "SELECT created_at, score, utilization FROM saved_checks "
                "WHERE user_id = ? ORDER BY created_at ASC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [{"date": r["created_at"], "score": r["score"],
                 "utilization": r["utilization"]} for r in rows]

    def count_checks(self, user_id: str) -> int:
        with get_conn(self._db) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM saved_checks WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return int(row["n"])
