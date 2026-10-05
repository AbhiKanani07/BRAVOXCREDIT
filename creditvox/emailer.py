"""
emailer.py
==========
Engagement-email plumbing. Fully built, intentionally DORMANT: with no email
provider configured it enqueues and "sends" via a no-op that just logs, so
nothing actually leaves your server until you wire a real provider.

When you're ready to send for real:
  1. Sign up with an email provider (Resend / SendGrid / AWS SES / Postmark).
  2. Verify bravoxcredit.com (SPF/DKIM DNS records) so mail isn't spam-filtered.
  3. Implement a provider Emailer subclass (sketch below) and set EMAIL_PROVIDER.
Legal: CAN-SPAM requires a physical mailing address and a working unsubscribe
link in every marketing email. The unsubscribe flow is already built
(/unsubscribe); add your address to the templates before you activate sending.

Pure stdlib.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from .storage import get_conn

log = logging.getLogger("creditvox.email")


# --- Drip sequence ---------------------------------------------------------
# (template id, subject, days after signup to send)
DRIP_SEQUENCE = [
    ("welcome", "Welcome to CreditVox — here's how to use it", 0),
    ("first_tip", "The one number that moves your approval odds most", 3),
    ("nudge_dashboard", "See the cards you're closest to qualifying for", 7),
    ("checkin", "A quick check-in on your credit goals", 21),
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- Pluggable sender ------------------------------------------------------
class Emailer(ABC):
    name = "base"

    @abstractmethod
    def send(self, to_email: str, subject: str, template: str) -> bool:
        """Return True if the message was accepted for delivery."""
        raise NotImplementedError


class NoopEmailer(Emailer):
    """Default. Logs what *would* be sent; never contacts a provider."""
    name = "noop"

    def send(self, to_email: str, subject: str, template: str) -> bool:
        log.info("EMAIL (noop) -> %s | template=%s | subject=%s",
                 to_email, template, subject)
        return True


class ProviderEmailerStub(Emailer):
    """
    Placeholder for a real provider. Sketch when you wire one:

        import httpx  # or the provider SDK
        resp = httpx.post("https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.email_api_key}"},
            json={"from": settings.from_email, "to": to_email,
                  "subject": subject, "html": render(template)})
        return resp.status_code < 300
    """
    name = "provider"

    def send(self, to_email: str, subject: str, template: str) -> bool:
        raise NotImplementedError(
            "Real email provider not wired. Keep EMAIL_PROVIDER=noop until you "
            "implement this against your provider's API."
        )


def get_emailer(name: str) -> Emailer:
    return {"noop": NoopEmailer, "provider": ProviderEmailerStub}.get(
        name, NoopEmailer)()


# --- Outbox operations -----------------------------------------------------
class EmailOutbox:
    def __init__(self, db_path: str, provider: str = "noop"):
        self._db = db_path
        self._provider = provider

    def enqueue_drip(self, user_id: str) -> int:
        """Queue the full welcome sequence for a new user. Idempotent per user:
        skips if this user already has queued drip messages."""
        now = _now()
        with get_conn(self._db) as conn:
            existing = conn.execute(
                "SELECT 1 FROM email_outbox WHERE user_id = ? LIMIT 1", (user_id,)
            ).fetchone()
            if existing:
                return 0
            for template, subject, day in DRIP_SEQUENCE:
                conn.execute(
                    "INSERT INTO email_outbox (outbox_id, user_id, template, "
                    "subject, scheduled_for, status, created_at) "
                    "VALUES (?,?,?,?,?, 'queued', ?)",
                    (str(uuid.uuid4()), user_id, template, subject,
                     (now + timedelta(days=day)).isoformat(), now.isoformat()),
                )
        return len(DRIP_SEQUENCE)

    def process_due(self, limit: int = 100) -> dict:
        """
        Send (or no-op) all due, queued messages for opted-in users. A cron job
        or worker calls this periodically; it's exposed via an admin endpoint.
        Respects email_opt_in so unsubscribes are honored.
        """
        emailer = get_emailer(self._provider)
        now_iso = _now().isoformat()
        sent = skipped = 0
        with get_conn(self._db) as conn:
            rows = conn.execute(
                "SELECT o.outbox_id, o.template, o.subject, u.email, u.email_opt_in "
                "FROM email_outbox o JOIN users u ON u.user_id = o.user_id "
                "WHERE o.status = 'queued' AND o.scheduled_for <= ? LIMIT ?",
                (now_iso, limit),
            ).fetchall()
            for r in rows:
                if not r["email_opt_in"]:
                    conn.execute("UPDATE email_outbox SET status='skipped' "
                                 "WHERE outbox_id=?", (r["outbox_id"],))
                    skipped += 1
                    continue
                ok = emailer.send(r["email"], r["subject"], r["template"])
                conn.execute("UPDATE email_outbox SET status=? WHERE outbox_id=?",
                             ("sent" if ok else "failed", r["outbox_id"]))
                sent += 1 if ok else 0
        return {"provider": emailer.name, "sent": sent, "skipped": skipped,
                "considered": len(rows)}
