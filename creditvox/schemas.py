"""
schemas.py
==========
Pydantic models for the HTTP boundary. These validate/serialize; the domain
core (engine, providers, audit) stays pydantic-free.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class ProfileIn(BaseModel):
    score: int = Field(..., ge=300, le=850)
    utilization: float = Field(..., ge=0, le=100)
    inquiries: int = Field(..., ge=0, le=50)
    new_accounts_24mo: Optional[int] = Field(None, ge=0, le=50)


class ConsentIn(BaseModel):
    consumer_token: str = Field(
        ..., min_length=6, max_length=128,
        description="Opaque consumer/session id your app assigns. NEVER an SSN.",
    )
    purpose: Literal["prequalification"] = "prequalification"
    agreed: bool = Field(..., description="Must be true; represents the consumer clicking 'I agree'.")


class ConsentOut(BaseModel):
    consent_id: str
    purpose: str
    disclosure_version: str
    expires_at: str


class ReportMatchIn(BaseModel):
    consent_id: str = Field(..., description="Returned by POST /v1/consent.")
    consumer_token: str = Field(..., min_length=6, max_length=128)
    # Only used when the active provider is 'manual'.
    manual_profile: Optional[ProfileIn] = None
    rank_by: Literal["approval", "fit"] = "approval"
    include_ineligible: bool = False


class MatchResult(BaseModel):
    card_id: str
    card_name: str
    issuer: str
    category: str
    annual_fee: int
    image_url: str = ""
    eligible: bool
    approval_probability: float
    fit_score: float
    match_label: str
    reasons: list[str]


class ReportMeta(BaseModel):
    report_id: str
    source: str
    inquiry_type: str
    pulled_at: str
    extras: dict


class ReportMatchOut(BaseModel):
    report: ReportMeta
    matches: list[MatchResult]


class FeedbackIn(BaseModel):
    consumer_token: str = Field(..., min_length=6, max_length=128)
    card_id: str = Field(..., min_length=1, max_length=64)
    outcome: Literal["applied", "approved", "denied"]
    # Optional: the profile/report context you showed, for calibration.
    score: Optional[int] = Field(None, ge=300, le=850)
    utilization: Optional[float] = Field(None, ge=0, le=100)
    inquiries: Optional[int] = Field(None, ge=0, le=50)
    new_accounts_24mo: Optional[int] = Field(None, ge=0, le=50)
    report_id: Optional[str] = None
    approval_probability_shown: Optional[float] = Field(None, ge=0, le=100)
    fit_score_shown: Optional[float] = Field(None, ge=0, le=100)
    notes: Optional[str] = Field(None, max_length=500)


class FeedbackOut(BaseModel):
    feedback_id: str
    recorded: bool = True


class CardStat(BaseModel):
    card_id: str
    applied: int
    approved: int
    denied: int
    total: int
    approval_rate: Optional[float] = None
