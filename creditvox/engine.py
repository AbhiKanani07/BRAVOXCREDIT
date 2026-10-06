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

from dataclasses import dataclass, asdict, field, replace
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
class CardFeatures:
    """Marketing/benefit data shown to users. Hand-entered today; shaped exactly
    like an affiliate data feed so a feed can populate it later with no code
    change. `last_updated` makes staleness visible instead of hidden."""
    rewards: str = ""           # concise earning summary
    signup_bonus: str = ""      # welcome offer (the most volatile field)
    intro_apr: str = ""         # intro APR offer, if any
    highlight: str = ""         # one-line "why this card"
    affiliate_url: str = ""     # referral link (dormant until you join a program)
    last_updated: str = ""      # ISO date the data was last verified


@dataclass(frozen=True)
class Card:
    id: str
    name: str
    issuer: str
    category: str            # starter | student | cash_back | travel
    min_tier: ScoreTier      # hard gate
    max_utilization: float   # max aggregate utilization % the profile tolerates
    max_inquiries: int       # max hard inquiries (trailing ~24mo) tolerated
    annual_fee: int = 0
    chase_5_24: bool = False  # optional issuer rule: auto-deny if >=5 new accts/24mo
    image_url: str = ""       # licensed card art URL; blank -> frontend placeholder
    features: CardFeatures = field(default_factory=CardFeatures)
    # Which bureau this issuer *typically* pulls (approximate; varies by state and
    # applicant). Values: experian | equifax | transunion | all | none.
    pull_bureau: str = "experian"
    pull_model: str = "FICO"  # issuers underwrite mostly on FICO, not VantageScore

    def to_dict(self) -> dict:
        d = asdict(self)              # recurses into CardFeatures automatically
        d["min_tier"] = self.min_tier.name
        return d


# NOTE: thresholds are heuristic estimates from public community data, not
# published issuer cutoffs. To add a card, copy a block and fill it in. To add
# real card art later, set image_url to a licensed image (from your affiliate
# program); blank renders a generated placeholder in the frontend.
CARD_MATRIX: list[Card] = [
    # --- Secured / starter (POOR: 300-579) ---------------------------------
    Card(
        id="opensky_secured",
        name="OpenSky Secured Visa",
        issuer="Capital Bank",
        category="starter",
        min_tier=ScoreTier.POOR,
        max_utilization=95.0,   # no credit check product; extremely forgiving
        max_inquiries=10,
        annual_fee=35,
    ),
    Card(
        id="capone_platinum_secured",
        name="Capital One Platinum Secured",
        issuer="Capital One",
        category="starter",
        min_tier=ScoreTier.POOR,
        max_utilization=95.0,
        max_inquiries=6,
        annual_fee=0,
    ),
    Card(
        id="discover_it_secured",
        name="Discover it Secured",
        issuer="Discover",
        category="starter",
        min_tier=ScoreTier.POOR,
        max_utilization=90.0,
        max_inquiries=6,
        annual_fee=0,
    ),
    Card(
        id="capone_quicksilver_secured",
        name="Capital One Quicksilver Secured",
        issuer="Capital One",
        category="starter",
        min_tier=ScoreTier.POOR,
        max_utilization=90.0,
        max_inquiries=6,
        annual_fee=0,
    ),
    # --- Student (FAIR: 580-669) -------------------------------------------
    Card(
        id="discover_it_student",
        name="Discover it Student Cash Back",
        issuer="Discover",
        category="student",
        min_tier=ScoreTier.FAIR,
        max_utilization=60.0,
        max_inquiries=5,
        annual_fee=0,
    ),
    Card(
        id="capone_savorone_student",
        name="Capital One SavorOne Student",
        issuer="Capital One",
        category="student",
        min_tier=ScoreTier.FAIR,
        max_utilization=55.0,
        max_inquiries=5,
        annual_fee=0,
    ),
    Card(
        id="chase_freedom_rise",
        name="Chase Freedom Rise",
        issuer="Chase",
        category="student",
        min_tier=ScoreTier.FAIR,
        max_utilization=50.0,
        max_inquiries=5,
        annual_fee=0,
    ),
    Card(
        id="bofa_travel_student",
        name="BofA Travel Rewards for Students",
        issuer="Bank of America",
        category="student",
        min_tier=ScoreTier.FAIR,
        max_utilization=50.0,
        max_inquiries=5,
        annual_fee=0,
    ),
    # --- Fair-to-good unsecured cash back ----------------------------------
    Card(
        id="capone_platinum",
        name="Capital One Platinum",
        issuer="Capital One",
        category="starter",
        min_tier=ScoreTier.FAIR,
        max_utilization=70.0,
        max_inquiries=5,
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
    # --- Good (670-739) cash back ------------------------------------------
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
        id="capone_savorone",
        name="Capital One SavorOne",
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
        id="chase_freedom_flex",
        name="Chase Freedom Flex",
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
        id="citi_custom_cash",
        name="Citi Custom Cash",
        issuer="Citi",
        category="cash_back",
        min_tier=ScoreTier.GOOD,
        max_utilization=35.0,
        max_inquiries=5,
        annual_fee=0,
    ),
    Card(
        id="wells_active_cash",
        name="Wells Fargo Active Cash",
        issuer="Wells Fargo",
        category="cash_back",
        min_tier=ScoreTier.GOOD,
        max_utilization=35.0,
        max_inquiries=4,
        annual_fee=0,
    ),
    Card(
        id="bofa_customized_cash",
        name="BofA Customized Cash Rewards",
        issuer="Bank of America",
        category="cash_back",
        min_tier=ScoreTier.GOOD,
        max_utilization=35.0,
        max_inquiries=4,
        annual_fee=0,
    ),
    Card(
        id="amex_blue_cash_everyday",
        name="Amex Blue Cash Everyday",
        issuer="American Express",
        category="cash_back",
        min_tier=ScoreTier.GOOD,
        max_utilization=30.0,
        max_inquiries=4,
        annual_fee=0,
    ),
    Card(
        id="capone_ventureone",
        name="Capital One VentureOne",
        issuer="Capital One",
        category="travel",
        min_tier=ScoreTier.GOOD,
        max_utilization=35.0,
        max_inquiries=4,
        annual_fee=0,
    ),
    # --- Very good (740-799) travel ----------------------------------------
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
        id="capone_venture",
        name="Capital One Venture",
        issuer="Capital One",
        category="travel",
        min_tier=ScoreTier.VERY_GOOD,
        max_utilization=30.0,
        max_inquiries=4,
        annual_fee=95,
    ),
    Card(
        id="citi_strata_premier",
        name="Citi Strata Premier",
        issuer="Citi",
        category="travel",
        min_tier=ScoreTier.VERY_GOOD,
        max_utilization=25.0,
        max_inquiries=5,
        annual_fee=95,
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
    # --- Excellent (800+) premium ------------------------------------------
    Card(
        id="capone_venture_x",
        name="Capital One Venture X",
        issuer="Capital One",
        category="travel",
        min_tier=ScoreTier.EXCELLENT,
        max_utilization=20.0,
        max_inquiries=4,
        annual_fee=395,
    ),
    Card(
        id="chase_sapphire_reserve",
        name="Chase Sapphire Reserve",
        issuer="Chase",
        category="travel",
        min_tier=ScoreTier.EXCELLENT,
        max_utilization=15.0,
        max_inquiries=3,
        annual_fee=795,   # relaunched 2026 ($550 -> $795)
        chase_5_24=True,
    ),
    Card(
        id="amex_platinum",
        name="American Express Platinum",
        issuer="American Express",
        category="travel",
        min_tier=ScoreTier.EXCELLENT,
        max_utilization=20.0,
        max_inquiries=4,
        annual_fee=695,
    ),
]


# ---------------------------------------------------------------------------
# 2b. CARD FEATURES
# ---------------------------------------------------------------------------
# Keyed by card id and merged into the matrix below. This is the single place
# to edit marketing data, and the shape an affiliate feed would populate.
# Base earning structures are stable; signup bonuses drift monthly, so where a
# figure isn't freshly verified it says "See issuer for current offer".
_TODAY = "2026-10-05"

CARD_FEATURES: dict[str, CardFeatures] = {
    "opensky_secured": CardFeatures(
        rewards="No rewards; builds credit with a refundable deposit",
        signup_bonus="None", intro_apr="None",
        highlight="No credit check to apply — an accessible starting point.",
        last_updated=_TODAY),
    "capone_platinum_secured": CardFeatures(
        rewards="No rewards; a path toward an unsecured card",
        signup_bonus="None", intro_apr="None",
        highlight="Refundable deposit from $49 with automatic credit-line reviews.",
        last_updated=_TODAY),
    "discover_it_secured": CardFeatures(
        rewards="2% at gas & dining (up to $1,000/qtr), 1% elsewhere",
        signup_bonus="First-year Cashback Match doubles all cash back",
        intro_apr="None",
        highlight="Rare rewards on a secured card, plus Cashback Match.",
        last_updated=_TODAY),
    "capone_quicksilver_secured": CardFeatures(
        rewards="1.5% cash back on every purchase",
        signup_bonus="None", intro_apr="None",
        highlight="Flat-rate rewards while you rebuild credit.",
        last_updated=_TODAY),
    "discover_it_student": CardFeatures(
        rewards="5% rotating categories (activate, up to $1,500/qtr), 1% elsewhere",
        signup_bonus="First-year Cashback Match doubles all cash back",
        intro_apr="See issuer for current intro APR",
        highlight="Strong rewards for students building credit.",
        last_updated=_TODAY),
    "capone_savorone_student": CardFeatures(
        rewards="3% dining, entertainment, streaming & groceries; 1% elsewhere",
        signup_bonus="See issuer for current offer", intro_apr="None",
        highlight="Great everyday rewards for students; no annual fee.",
        last_updated=_TODAY),
    "chase_freedom_rise": CardFeatures(
        rewards="1.5% cash back on all purchases",
        signup_bonus="See issuer for current offer", intro_apr="None",
        highlight="Designed as a first Chase card to start your credit journey.",
        last_updated=_TODAY),
    "bofa_travel_student": CardFeatures(
        rewards="1.5x points on all purchases, points never expire",
        signup_bonus="See issuer for current offer", intro_apr="See issuer",
        highlight="No annual fee and no foreign transaction fees.",
        last_updated=_TODAY),
    "capone_platinum": CardFeatures(
        rewards="No rewards; unsecured credit building",
        signup_bonus="None", intro_apr="None",
        highlight="Unsecured starter card with automatic credit-line reviews.",
        last_updated=_TODAY),
    "discover_it_cashback": CardFeatures(
        rewards="5% rotating categories (activate, up to $1,500/qtr), 1% elsewhere",
        signup_bonus="First-year Cashback Match doubles all cash back",
        intro_apr="See issuer for current intro APR",
        highlight="Top no-fee rotating-category card with Cashback Match.",
        last_updated=_TODAY),
    "capone_quicksilver": CardFeatures(
        rewards="1.5% on every purchase; 5% on Capital One Travel",
        signup_bonus="See issuer for current offer", intro_apr="See issuer",
        highlight="Simple flat-rate cash back, no annual fee.",
        last_updated=_TODAY),
    "capone_savorone": CardFeatures(
        rewards="3% dining, entertainment, streaming & groceries; 1% elsewhere",
        signup_bonus="See issuer for current offer", intro_apr="See issuer",
        highlight="Best no-fee card for dining and going out.",
        last_updated=_TODAY),
    "chase_freedom_unlimited": CardFeatures(
        rewards="1.5% all purchases, 5% Chase Travel, 3% dining & drugstores",
        signup_bonus="$200 after $500 spend in first 3 months", intro_apr="None",
        highlight="A 1.5% floor on everything plus Chase bonus categories.",
        last_updated=_TODAY),
    "chase_freedom_flex": CardFeatures(
        rewards="5% rotating (activate, up to $1,500/qtr), 5% Chase Travel, "
                "3% dining & drugstores, 1% elsewhere",
        signup_bonus="$200 after $500 spend in first 3 months", intro_apr="None",
        highlight="Rotating 5% categories with no annual fee.",
        last_updated=_TODAY),
    "citi_double_cash": CardFeatures(
        rewards="2% total — 1% when you buy, 1% as you pay it off",
        signup_bonus="See issuer for current offer", intro_apr="See issuer",
        highlight="The simplest high flat-rate cash back.",
        last_updated=_TODAY),
    "citi_custom_cash": CardFeatures(
        rewards="5% on your top eligible category each cycle (up to $500), 1% else",
        signup_bonus="See issuer for current offer", intro_apr="See issuer",
        highlight="Automatically rewards your top spending category.",
        last_updated=_TODAY),
    "wells_active_cash": CardFeatures(
        rewards="2% cash back on all purchases",
        signup_bonus="See issuer for current offer", intro_apr="See issuer",
        highlight="Flat 2% on everything with no annual fee.",
        last_updated=_TODAY),
    "bofa_customized_cash": CardFeatures(
        rewards="3% in a category you choose + 2% groceries/wholesale "
                "(up to $2,500/qtr combined), 1% elsewhere",
        signup_bonus="See issuer for current offer", intro_apr="See issuer",
        highlight="Pick your 3% category; boosted with a BofA relationship.",
        last_updated=_TODAY),
    "amex_blue_cash_everyday": CardFeatures(
        rewards="3% US supermarkets, US online retail & US gas "
                "(up to $6,000/yr each), 1% elsewhere",
        signup_bonus="See issuer for current offer", intro_apr="See issuer",
        highlight="Everyday cash back for groceries and gas, no annual fee.",
        last_updated=_TODAY),
    "capone_ventureone": CardFeatures(
        rewards="1.25x miles on all purchases, 5x on Capital One Travel",
        signup_bonus="See issuer for current offer", intro_apr="See issuer",
        highlight="No-fee entry into Capital One miles.",
        last_updated=_TODAY),
    "chase_sapphire_preferred": CardFeatures(
        rewards="5x Chase Travel; 3x dining, streaming, online groceries, gas & "
                "EV charging, and vacation rentals; 2x other travel; 1x elsewhere",
        signup_bonus="75,000 points after $5,000 in 3 months "
                     "(100k limited-time offer may apply)",
        intro_apr="None",
        highlight="Refreshed June 2026: new 3x categories, $100 hotel credit, Apple TV.",
        last_updated=_TODAY),
    "capone_venture": CardFeatures(
        rewards="2x miles on every purchase, 5x on Capital One Travel",
        signup_bonus="See issuer for current offer", intro_apr="None",
        highlight="Simple 2x miles on everything for travelers.",
        last_updated=_TODAY),
    "citi_strata_premier": CardFeatures(
        rewards="3x air travel, hotels, restaurants, supermarkets, gas & EV "
                "charging; 1x elsewhere",
        signup_bonus="See issuer for current offer", intro_apr="None",
        highlight="Broad 3x categories including everyday spend.",
        last_updated=_TODAY),
    "amex_gold": CardFeatures(
        rewards="4x dining worldwide (up to $50k/yr), 4x US supermarkets "
                "(up to $25k/yr), 3x flights, 5x prepaid hotels via Amex Travel",
        signup_bonus="100,000 points after $8,000 in first 6 months",
        intro_apr="None",
        highlight="Up to ~$424 in annual credits offset the $325 fee.",
        last_updated=_TODAY),
    "capone_venture_x": CardFeatures(
        rewards="2x miles on everything; 10x hotels & 5x flights via Capital One Travel",
        signup_bonus="See issuer for current offer", intro_apr="None",
        highlight="$300 annual travel credit + 10,000 anniversary miles offset most of the fee.",
        last_updated=_TODAY),
    "chase_sapphire_reserve": CardFeatures(
        rewards="8x Chase Travel, 4x flights & hotels booked direct, 3x dining; 1x else",
        signup_bonus="See issuer for current offer", intro_apr="None",
        highlight="Relaunched 2026 at a $795 fee with expanded credits and lounge access.",
        last_updated=_TODAY),
    "amex_platinum": CardFeatures(
        rewards="5x flights (direct or via Amex Travel) & 5x prepaid hotels via Amex Travel; 1x else",
        signup_bonus="See issuer for current offer", intro_apr="None",
        highlight="Premium travel: lounges, hotel status, and ~$1,500 in annual credits.",
        last_updated=_TODAY),
}

# Typical pull bureau by issuer, from public community data (Doctor of Credit,
# myFICO). APPROXIMATE: issuers don't disclose this, and it varies by state and
# applicant. Amex ~Experian, BofA ~Experian, Chase/Citi/Discover/WF lean Experian,
# Capital One pulls all three; OpenSky is no-credit-check.
ISSUER_PULL = {
    "American Express": "experian",
    "Chase": "experian",
    "Citi": "experian",
    "Discover": "experian",
    "Wells Fargo": "experian",
    "Bank of America": "experian",
    "Capital One": "all",
    "Capital Bank": "none",
}

# Official issuer credit-card pages. These are NOT affiliate-tracked links — they
# make the Apply buttons functional now. Replace each with your affiliate deep-link
# (per card, in CARD_FEATURES[...].affiliate_url) once you join an affiliate program.
ISSUER_APPLY = {
    "American Express": "https://www.americanexpress.com/us/credit-cards/",
    "Chase": "https://www.chase.com/personal/credit-cards",
    "Citi": "https://www.citi.com/credit-cards/",
    "Discover": "https://www.discover.com/credit-cards/",
    "Wells Fargo": "https://www.wellsfargo.com/credit-cards/",
    "Bank of America": "https://www.bankofamerica.com/credit-cards/",
    "Capital One": "https://www.capitalone.com/credit-cards/",
    "Capital Bank": "https://www.openskycc.com/",
}

# Merge features (+ apply URL) + pull bureau into the matrix (frozen -> replace()).
def _merge_card(c):
    feat = CARD_FEATURES.get(c.id, c.features)
    if not feat.affiliate_url:   # don't clobber a real affiliate link if set
        feat = replace(feat, affiliate_url=ISSUER_APPLY.get(c.issuer, ""))
    return replace(c, features=feat, pull_bureau=ISSUER_PULL.get(c.issuer, "experian"))

CARD_MATRIX = [_merge_card(c) for c in CARD_MATRIX]


# ---------------------------------------------------------------------------
# 3. USER PROFILE
# ---------------------------------------------------------------------------

@dataclass
class UserProfile:
    score: int                              # primary / fallback score (300-850)
    utilization: float                      # aggregate util %, e.g. 12.5
    inquiries: int                          # hard inquiries, trailing ~24mo
    new_accounts_24mo: Optional[int] = None  # optional; only used for 5/24 cards
    # Optional per-bureau scores. When provided, each card is evaluated against
    # the score at the bureau that issuer typically pulls. Left blank -> `score`.
    experian: Optional[int] = None
    equifax: Optional[int] = None
    transunion: Optional[int] = None
    model: str = "FICO"                     # "FICO" | "VantageScore" (informational)

    def score_for(self, bureau: str) -> int:
        """The score to evaluate against for a card whose issuer pulls `bureau`.
        'all' (e.g. Capital One, which triple-pulls) uses the weakest provided
        bureau score — the conservative, honest reading. Missing data falls back
        to the primary score."""
        per = {"experian": self.experian, "equifax": self.equifax,
               "transunion": self.transunion}
        if bureau == "all":
            vals = [v for v in per.values() if v is not None]
            return min(vals) if vals else self.score
        if bureau in per and per[bureau] is not None:
            return per[bureau]
        return self.score

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
    # Evaluate against the score at the bureau this issuer typically pulls.
    eff_score = profile.score_for(card.pull_bureau)
    user_tier = score_to_tier(eff_score)
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
            "image_url": card.image_url,
            "features": asdict(card.features),
            "pull_bureau": card.pull_bureau,
            "pull_model": card.pull_model,
            "score_used": eff_score,
            "eligible": False,
            "approval_probability": 0.0,
            "fit_score": 0.0,
            "match_label": "Not Eligible",
            "reasons": reasons,
        }

    # --- Margin scoring (how *safely* did they clear each threshold) ---------
    score_factor = _clamp(
        (eff_score - TIER_FLOOR[card.min_tier]) / SCORE_RUNWAY
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
        "image_url": card.image_url,
        "features": asdict(card.features),
        "pull_bureau": card.pull_bureau,
        "pull_model": card.pull_model,
        "score_used": eff_score,
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


# Thresholds for what counts as "almost qualifying" — close enough to be an
# actionable nudge rather than a far-off aspiration.
NEAR_SCORE_POINTS = 25     # within this many points of the required tier floor
NEAR_UTIL_POINTS = 15      # within this many utilization points over the cap
NEAR_INQ_COUNT = 1         # within this many inquiries over the cap


def near_misses(profile: UserProfile, matrix: list[Card] = CARD_MATRIX) -> list[dict]:
    """
    Cards the user does NOT yet qualify for but is close to — with a concrete,
    single-step hint for each. Powers the dashboard's "almost there" nudges.
    A card qualifies as a near miss only if every failing factor is within a
    small, achievable margin (so we never nudge someone toward a card that's
    realistically years away).
    """
    out: list[dict] = []

    for card in matrix:
        eff_score = profile.score_for(card.pull_bureau)
        user_tier = score_to_tier(eff_score)
        hints: list[str] = []
        achievable = True

        # Score: only "near" if within NEAR_SCORE_POINTS of the required floor.
        if user_tier < card.min_tier:
            floor = TIER_FLOOR[card.min_tier]
            gap = floor - eff_score
            if 0 < gap <= NEAR_SCORE_POINTS:
                hints.append(f"Raise your score ~{gap} points")
            else:
                achievable = False

        # Utilization: near if only modestly over the cap.
        if profile.utilization > card.max_utilization:
            over = profile.utilization - card.max_utilization
            if over <= NEAR_UTIL_POINTS:
                hints.append(f"Lower utilization to {card.max_utilization:.0f}% or below")
            else:
                achievable = False

        # Inquiries: near if just one too many (they age off over time).
        if profile.inquiries > card.max_inquiries:
            if profile.inquiries - card.max_inquiries <= NEAR_INQ_COUNT:
                hints.append("Wait for an inquiry to age off")
            else:
                achievable = False

        # 5/24 and other hard structural blocks aren't "near misses."
        if card.chase_5_24 and profile.new_accounts_24mo is not None \
                and profile.new_accounts_24mo >= 5:
            achievable = False

        if hints and achievable:
            out.append({
                "card_id": card.id,
                "card_name": card.name,
                "issuer": card.issuer,
                "category": card.category,
                "annual_fee": card.annual_fee,
                "image_url": card.image_url,
                "features": asdict(card.features),
                "hints": hints,
            })

    # Fewest steps first = most achievable first.
    out.sort(key=lambda c: len(c["hints"]))
    return out


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
