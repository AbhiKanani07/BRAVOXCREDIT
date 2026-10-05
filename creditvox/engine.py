"""
card_match_engine.py
====================
A zero-dependency credit-card pre-approval rules engine.

No SSNs, no soft/hard pulls, no bureau integration. Pure heuristic matching
against a hardcoded underwriting matrix, designed to be wrapped in a thin API
layer (FastAPI / Flask) later. Every public object is JSON-serializable.

IMPORTANT (read before shipping):
    The thresholds in CARD_MATRIX are HEURISTIC ESTIMATES aggregated from public
    community data (Doctor of Credit, r/CreditCards, myFICO forums). Issuers do
    NOT publish exact underwriting cutoffs, and real approvals also weigh income,
    DTI, account age, existing banking relationship, and internal risk models
    this engine cannot see. Treat every output as an educated confidence score,
    never a guarantee. This is a directional tool, not underwriting.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import IntEnum
from typing import Optional


# ---------------------------------------------------------------------------
# 1. SCORE TIERS
# ---------------------------------------------------------------------------
# Issuers reason in tiers, not raw points. We keep both: tiers drive the hard
# eligibility gate; raw points drive the "how safely did they clear it" margin.

class ScoreTier(IntEnum):
    POOR = 0        # 300-579
    FAIR = 1        # 580-669
    GOOD = 2        # 670-739
    VERY_GOOD = 3   # 740-799
    EXCELLENT = 4   # 800-850


TIER_FLOOR = {
    ScoreTier.POOR: 300,
    ScoreTier.FAIR: 580,
    ScoreTier.GOOD: 670,
    ScoreTier.VERY_GOOD: 740,
    ScoreTier.EXCELLENT: 800,
}


def score_to_tier(score: int) -> ScoreTier:
    if score >= 800:
        return ScoreTier.EXCELLENT
    if score >= 740:
        return ScoreTier.VERY_GOOD
    if score >= 670:
        return ScoreTier.GOOD
    if score >= 580:
        return ScoreTier.FAIR
    return ScoreTier.POOR


# ---------------------------------------------------------------------------
# 2. DATA MATRIX
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Card:
    id: str
    name: str
    issuer: str
    category: str            # starter | cash_back | travel
    min_tier: ScoreTier      # hard gate
    max_utilization: float   # max aggregate utilization % the profile tolerates
    max_inquiries: int       # max hard inquiries (trailing ~24mo) tolerated
    annual_fee: int = 0
    chase_5_24: bool = False  # optional issuer rule: auto-deny if >=5 new accts/24mo

    def to_dict(self) -> dict:
        d = asdict(self)
        d["min_tier"] = self.min_tier.name
        return d


CARD_MATRIX: list[Card] = [
    Card(
        id="capone_platinum_secured",
        name="Capital One Platinum Secured",
        issuer="Capital One",
        category="starter",
        min_tier=ScoreTier.POOR,
        max_utilization=95.0,   # secured/starter product is very forgiving
        max_inquiries=6,
        annual_fee=0,
    ),
    Card(
        id="discover_it_cashback",
        name="Discover it Cash Back",
        issuer="Discover",
        category="cash_back",
        min_tier=ScoreTier.FAIR,
        max_utilization=50.0,
        max_inquiries=5,
        annual_fee=0,
    ),
    Card(
        id="capone_quicksilver",
        name="Capital One Quicksilver",
        issuer="Capital One",
        category="cash_back",
        min_tier=ScoreTier.GOOD,
        max_utilization=40.0,
        max_inquiries=4,
        annual_fee=0,
    ),
    Card(
        id="chase_freedom_unlimited",
        name="Chase Freedom Unlimited",
        issuer="Chase",
        category="cash_back",
        min_tier=ScoreTier.GOOD,
        max_utilization=30.0,
        max_inquiries=4,
        annual_fee=0,
        chase_5_24=True,
    ),
    Card(
        id="citi_double_cash",
        name="Citi Double Cash",
        issuer="Citi",
        category="cash_back",
        min_tier=ScoreTier.GOOD,
        max_utilization=30.0,
        max_inquiries=5,
        annual_fee=0,
    ),
    Card(
        id="chase_sapphire_preferred",
        name="Chase Sapphire Preferred",
        issuer="Chase",
        category="travel",
        min_tier=ScoreTier.VERY_GOOD,
        max_utilization=25.0,
        max_inquiries=3,
        annual_fee=95,
        chase_5_24=True,
    ),
    Card(
        id="amex_gold",
        name="American Express Gold",
        issuer="American Express",
        category="travel",
        min_tier=ScoreTier.VERY_GOOD,
        max_utilization=25.0,
        max_inquiries=4,
        annual_fee=325,
    ),
    Card(
        id="chase_sapphire_reserve",
        name="Chase Sapphire Reserve",
        issuer="Chase",
        category="travel",
        min_tier=ScoreTier.EXCELLENT,
        max_utilization=15.0,
        max_inquiries=3,
        annual_fee=550,
        chase_5_24=True,
    ),
]


# ---------------------------------------------------------------------------
# 3. USER PROFILE
# ---------------------------------------------------------------------------

@dataclass
class UserProfile:
    score: int                              # e.g. 300-850
    utilization: float                      # aggregate util %, e.g. 12.5
    inquiries: int                          # hard inquiries, trailing ~24mo
    new_accounts_24mo: Optional[int] = None  # optional; only used for 5/24 cards

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 4. MATCHING ENGINE
# ---------------------------------------------------------------------------

# Tuning knobs. Pulled out as module constants so you can A/B them behind the
# API without touching the logic.
SCORE_RUNWAY = 60.0        # points above the card's tier floor that earn full marks
WEIGHT_SCORE = 0.50        # score is the dominant signal
WEIGHT_UTIL = 0.30
WEIGHT_INQ = 0.20
PROB_FLOOR = 45.0          # a bare pass is never a coin flip in the user's favor
PROB_CEIL = 96.0           # never promise ~100%; other factors are invisible to us

# Tier-fit: a SEPARATE objective from approval likelihood. approval_probability
# answers "will they say yes?"; fit_score answers "is this the right card for
# this profile?" A prime user is near-certain to get a secured starter card
# (high approval), but it's a poor recommendation (low fit). Each tier the card
# sits BELOW the user costs PENALTY_PER_TIER of the probability, floored so a
# mismatch is demoted, never zeroed.
PENALTY_PER_TIER = 0.12
FIT_FLOOR = 0.30


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _label(prob: float) -> str:
    if prob >= 85:
        return "Excellent Match"
    if prob >= 70:
        return "Strong Match"
    if prob >= 55:
        return "Moderate Match"
    return "Low Match"


def evaluate_card(profile: UserProfile, card: Card) -> dict:
    """
    Evaluate one profile against one card.

    Returns a JSON-serializable dict with eligibility, a 0-100 approval
    probability, a human label, and per-filter reasons (for the "why / why not"
    UI that a real pre-approval tool shows).
    """
    user_tier = score_to_tier(profile.score)
    reasons: list[str] = []
    eligible = True

    # --- Hard gates ---------------------------------------------------------
    if user_tier < card.min_tier:
        eligible = False
        reasons.append(
            f"Score tier {user_tier.name} below required {card.min_tier.name}"
        )

    if profile.utilization > card.max_utilization:
        eligible = False
        reasons.append(
            f"Utilization {profile.utilization:.0f}% exceeds max {card.max_utilization:.0f}%"
        )

    if profile.inquiries > card.max_inquiries:
        eligible = False
        reasons.append(
            f"{profile.inquiries} inquiries exceed max {card.max_inquiries}"
        )

    # Optional issuer-specific rule: Chase 5/24. Only enforced when the card
    # opts in AND the caller supplied new-account data. Missing data => skip,
    # so the core 3-input profile keeps working unchanged.
    if card.chase_5_24 and profile.new_accounts_24mo is not None:
        if profile.new_accounts_24mo >= 5:
            eligible = False
            reasons.append(
                f"Chase 5/24: {profile.new_accounts_24mo} new accounts in 24mo (limit 5)"
            )

    if not eligible:
        return {
            "card_id": card.id,
            "card_name": card.name,
            "issuer": card.issuer,
            "category": card.category,
            "annual_fee": card.annual_fee,
            "eligible": False,
            "approval_probability": 0.0,
            "fit_score": 0.0,
            "match_label": "Not Eligible",
            "reasons": reasons,
        }

    # --- Margin scoring (how *safely* did they clear each threshold) ---------
    score_factor = _clamp(
        (profile.score - TIER_FLOOR[card.min_tier]) / SCORE_RUNWAY
    )

    # Relative headroom under the utilization ceiling. Lower util => higher.
    util_factor = _clamp(
        (card.max_utilization - profile.utilization) / card.max_utilization
    )

    # Relative headroom under the inquiry ceiling.
    if card.max_inquiries > 0:
        inq_factor = _clamp(
            (card.max_inquiries - profile.inquiries) / card.max_inquiries
        )
    else:
        inq_factor = 1.0 if profile.inquiries == 0 else 0.0

    raw = (
        WEIGHT_SCORE * score_factor
        + WEIGHT_UTIL * util_factor
        + WEIGHT_INQ * inq_factor
    )
    probability = round(PROB_FLOOR + raw * (PROB_CEIL - PROB_FLOOR), 1)

    # Tier-fit: penalize cards sitting below the user's tier (eligible cards
    # always have user_tier >= min_tier, so tier_distance is >= 0).
    tier_distance = int(user_tier) - int(card.min_tier)
    fit_multiplier = _clamp(1.0 - PENALTY_PER_TIER * tier_distance, FIT_FLOOR, 1.0)
    fit_score = round(probability * fit_multiplier, 1)

    reasons.append(f"Clears all filters (score signal {score_factor:.2f}, "
                   f"util {util_factor:.2f}, inquiries {inq_factor:.2f})")

    return {
        "card_id": card.id,
        "card_name": card.name,
        "issuer": card.issuer,
        "category": card.category,
        "annual_fee": card.annual_fee,
        "eligible": True,
        "approval_probability": probability,
        "fit_score": fit_score,
        "match_label": _label(probability),
        "reasons": reasons,
    }


def match_cards(
    profile: UserProfile,
    matrix: list[Card] = CARD_MATRIX,
    include_ineligible: bool = False,
    rank_by: str = "approval",
) -> list[dict]:
    """
    Run a profile against the whole matrix.

    rank_by:
        "approval" (default) -> sort by raw approval likelihood. Answers
                                "which am I most likely to get?" (easy cards win).
        "fit"                -> sort by tier-fit recommendation score. Answers
                                "which is the right card for me?" (demotes cards
                                far below the user's tier).

    Set include_ineligible=True to also return the misses with their reasons,
    which is what powers a Credit-Karma-style "not likely" section.
    """
    if rank_by not in ("approval", "fit"):
        raise ValueError("rank_by must be 'approval' or 'fit'")
    sort_key = "approval_probability" if rank_by == "approval" else "fit_score"

    results = [evaluate_card(profile, c) for c in matrix]
    eligible = [r for r in results if r["eligible"]]
    eligible.sort(key=lambda r: r[sort_key], reverse=True)

    if include_ineligible:
        ineligible = [r for r in results if not r["eligible"]]
        return eligible + ineligible
    return eligible


# ---------------------------------------------------------------------------
# 5. DEMO
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    profiles = {
        "Thin-file starter (620, 45% util, 2 inq)":
            UserProfile(score=620, utilization=45.0, inquiries=2),
        "Solid Good-tier (710, 15% util, 1 inq)":
            UserProfile(score=710, utilization=15.0, inquiries=1),
        "Prime, over 5/24 (780, 6% util, 2 inq, 6 new accts)":
            UserProfile(score=780, utilization=6.0, inquiries=2, new_accounts_24mo=6),
        "Top-tier clean (810, 4% util, 0 inq, 1 new acct)":
            UserProfile(score=810, utilization=4.0, inquiries=0, new_accounts_24mo=1),
    }

    for name, profile in profiles.items():
        print("=" * 72)
        print(name)
        print("-" * 72)
        print("  Ranked by APPROVAL likelihood        |  Ranked by FIT")
        by_appr = match_cards(profile, rank_by="approval")
        by_fit = match_cards(profile, rank_by="fit")
        for a, f in zip(by_appr, by_fit):
            left = f"{a['approval_probability']:>5.1f}%  {a['card_name']}"
            right = f"{f['fit_score']:>5.1f}  {f['card_name']}"
            print(f"  {left:<36} |  {right}")
        print()

    print("=" * 72)
    print("Full JSON payload (top-tier profile, incl. ineligible):")
    print(json.dumps(
        match_cards(profiles["Top-tier clean (810, 4% util, 0 inq, 1 new acct)"],
                    include_ineligible=True),
        indent=2,
    ))
