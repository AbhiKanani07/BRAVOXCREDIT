"""
providers.py
============
The data-source abstraction. The rules engine only ever sees a normalized
CreditReport -> UserProfile; it never knows or cares whether the numbers came
from a form, a mock, or a real bureau aggregator.

Adding a real bureau later = implement one more CreditDataProvider subclass and
flip CREDIT_PROVIDER in config. Nothing else changes.

Pure stdlib.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from .audit import ConsentRecord, ConsentError
from .engine import UserProfile


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ApplicantRef:
    """
    Minimal, non-sensitive reference used to request a report.

    `token` is an OPAQUE consumer identifier your app assigns (e.g. a session
    or account id). It is NEVER an SSN — real bureau providers take PII over
    their own secure channel; this object only carries what our system needs.
    `manual_values` is populated only for the ManualProvider path.
    """
    token: str
    manual_values: Optional[dict] = None


@dataclass
class CreditReport:
    """Normalized report shape. Every provider maps its response into this."""
    report_id: str
    source: str              # "manual" | "mock_bureau:transunion" | ...
    inquiry_type: str        # "soft" (prequal) | "hard" (underwriting)
    pulled_at: str           # ISO 8601 UTC
    score: int
    utilization: float
    inquiries: int
    new_accounts_24mo: Optional[int] = None
    extras: dict = field(default_factory=dict)  # provider-specific normalized extras

    def to_profile(self) -> UserProfile:
        return UserProfile(
            score=self.score,
            utilization=self.utilization,
            inquiries=self.inquiries,
            new_accounts_24mo=self.new_accounts_24mo,
        )

    def to_dict(self) -> dict:
        return asdict(self)


class CreditDataProvider(ABC):
    name: str = "base"
    inquiry_type: str = "soft"

    @abstractmethod
    def fetch(self, applicant: ApplicantRef, consent: ConsentRecord) -> CreditReport:
        """Return a normalized CreditReport. Must honor `consent`."""
        raise NotImplementedError

    def _guard(self, consent: ConsentRecord) -> None:
        # Defense in depth: the API also checks consent, but a provider must
        # never pull without a valid prequalification consent.
        if not consent.is_valid("prequalification"):
            raise ConsentError("Provider refused: no valid prequalification consent.")


class ManualProvider(CreditDataProvider):
    """User-entered values. Always available, zero external dependency, free."""
    name = "manual"
    inquiry_type = "soft"

    def fetch(self, applicant: ApplicantRef, consent: ConsentRecord) -> CreditReport:
        self._guard(consent)
        vals = applicant.manual_values or {}
        missing = {"score", "utilization", "inquiries"} - vals.keys()
        if missing:
            raise ValueError(f"Manual provider missing fields: {sorted(missing)}")
        return CreditReport(
            report_id=f"manual-{applicant.token[:12]}",
            source="manual",
            inquiry_type=self.inquiry_type,
            pulled_at=_now_iso(),
            score=int(vals["score"]),
            utilization=float(vals["utilization"]),
            inquiries=int(vals["inquiries"]),
            new_accounts_24mo=vals.get("new_accounts_24mo"),
            extras={"entry": "self_reported"},
        )


class MockBureauProvider(CreditDataProvider):
    """
    Deterministic fake bureau report for building and testing the full flow
    end to end with NO vendor, NO cost, NO compliance exposure.

    Same `token` always yields the same report (seeded RNG), so your frontend
    and tests are reproducible. Swap this for RealBureauProvider when you have
    an aggregator sandbox key — the return shape is identical.
    """
    name = "mock_bureau:transunion"
    inquiry_type = "soft"

    def fetch(self, applicant: ApplicantRef, consent: ConsentRecord) -> CreditReport:
        self._guard(consent)
        rng = random.Random(applicant.token)  # deterministic per token
        score = rng.randint(560, 820)
        utilization = round(rng.uniform(1.0, 60.0), 1)
        inquiries = rng.randint(0, 6)
        new_accounts = rng.randint(0, 7)
        return CreditReport(
            report_id=f"mock-{rng.getrandbits(48):012x}",
            source=self.name,
            inquiry_type=self.inquiry_type,
            pulled_at=_now_iso(),
            score=score,
            utilization=utilization,
            inquiries=inquiries,
            new_accounts_24mo=new_accounts,
            extras={
                "model": "VantageScore 3.0 (simulated)",
                "bureau": "TransUnion (simulated)",
                "note": "SIMULATED DATA — not a real credit report",
            },
        )


class RealBureauProvider(CreditDataProvider):
    """
    Placeholder for a real aggregator (Array / iSoftpull / CRS / etc.).

    Implementation sketch when you have a sandbox key:
      1. POST the applicant's consented PII to the aggregator's soft-pull endpoint.
      2. Receive their JSON, then map THEIR fields into CreditReport below.
      3. Keep inquiry_type = "soft" for prequalification.
    Until wired, it refuses loudly so nothing silently ships fake-as-real.
    """
    name = "bureau"
    inquiry_type = "soft"

    def fetch(self, applicant: ApplicantRef, consent: ConsentRecord) -> CreditReport:
        raise NotImplementedError(
            "RealBureauProvider is not wired yet. Set CREDIT_PROVIDER=bureau_mock "
            "for development, or implement this against your aggregator sandbox."
        )


def get_provider(name: str) -> CreditDataProvider:
    table = {
        "manual": ManualProvider,
        "bureau_mock": MockBureauProvider,
        "bureau": RealBureauProvider,
    }
    if name not in table:
        raise ValueError(f"Unknown CREDIT_PROVIDER {name!r}; "
                         f"choose from {sorted(table)}")
    return table[name]()
