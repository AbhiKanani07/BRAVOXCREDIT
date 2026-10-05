"""Engine tests — pure stdlib, no external deps required to run these."""

from creditvox.engine import (
    CARD_MATRIX, UserProfile, ScoreTier, score_to_tier,
    match_cards, evaluate_card,
)


def test_score_tiers():
    assert score_to_tier(300) == ScoreTier.POOR
    assert score_to_tier(669) == ScoreTier.FAIR
    assert score_to_tier(670) == ScoreTier.GOOD
    assert score_to_tier(740) == ScoreTier.VERY_GOOD
    assert score_to_tier(800) == ScoreTier.EXCELLENT


def test_probability_bounds():
    # Any eligible card sits inside the configured band.
    p = UserProfile(score=810, utilization=1, inquiries=0)
    for m in match_cards(p):
        assert 45.0 <= m["approval_probability"] <= 96.0


def test_low_score_is_gated_out():
    weak = UserProfile(score=520, utilization=10, inquiries=0)  # POOR
    matches = match_cards(weak)
    # A POOR profile should only clear POOR-tier starter cards, never GOOD+.
    assert matches, "expected at least one starter card for a POOR profile"
    assert all(m["category"] == "starter" for m in matches)
    ids = {m["card_id"] for m in matches}
    assert "capone_platinum_secured" in ids          # known secured starter
    assert "citi_double_cash" not in ids             # a GOOD-tier card must not leak in
    assert "chase_sapphire_reserve" not in ids        # nor an EXCELLENT one


def test_utilization_hard_gate():
    over = UserProfile(score=760, utilization=90, inquiries=0)
    res = evaluate_card(over, next(c for c in CARD_MATRIX if c.id == "amex_gold"))
    assert res["eligible"] is False
    assert any("Utilization" in r for r in res["reasons"])


def test_chase_5_24_blocks_when_over():
    over = UserProfile(score=780, utilization=5, inquiries=1, new_accounts_24mo=6)
    ids = {m["card_id"] for m in match_cards(over)}
    assert "chase_freedom_unlimited" not in ids
    assert "chase_sapphire_preferred" not in ids


def test_5_24_skipped_when_not_provided():
    # Missing new_accounts_24mo must NOT block Chase cards.
    p = UserProfile(score=780, utilization=5, inquiries=1)
    ids = {m["card_id"] for m in match_cards(p)}
    assert "chase_freedom_unlimited" in ids


def test_fit_demotes_far_below_tier():
    prime = UserProfile(score=810, utilization=4, inquiries=0, new_accounts_24mo=1)
    by_fit = match_cards(prime, rank_by="fit")
    # Secured starter is a poor fit for a prime profile -> should not rank first.
    assert by_fit[0]["card_id"] != "capone_platinum_secured"


def test_include_ineligible_returns_reasons():
    weak = UserProfile(score=520, utilization=10, inquiries=0)
    full = match_cards(weak, include_ineligible=True)
    inelig = [m for m in full if not m["eligible"]]
    assert inelig and all(m["reasons"] for m in inelig)
