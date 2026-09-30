"""
api.py
======
The web layer. Wires the domain core (engine + providers + audit) behind HTTP
with production-shaped concerns: CORS for the frontend, request-id + timing
logs, uniform error envelopes, and the consent-gated pull flow.

Run from the project root (the folder that CONTAINS the creditvox/ package):
    uvicorn creditvox.api:app --reload
Docs (dev only): http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

from .config import settings
from .engine import CARD_MATRIX, UserProfile, match_cards
from .audit import AuditLogger, ConsentStore, ConsentError
from .feedback import FeedbackStore, FeedbackError
from .storage import init_db
from .providers import ApplicantRef, get_provider
from .schemas import (
    ConsentIn, ConsentOut, MatchResult, ProfileIn,
    ReportMatchIn, ReportMatchOut, FeedbackIn, FeedbackOut, CardStat,
)

# --- Logging ---------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("creditvox")

# --- Persistence + shared singletons ---------------------------------------
init_db(settings.db_path)
consent_store = ConsentStore(
    db_path=settings.db_path,
    ttl_minutes=settings.consent_ttl_minutes,
    disclosure_version=settings.disclosure_version,
)
audit = AuditLogger(settings.db_path)
feedback_store = FeedbackStore(settings.db_path)
_VALID_CARD_IDS = {c.id for c in CARD_MATRIX}

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# --- App --------------------------------------------------------------------
app = FastAPI(
    title="CreditVox API",
    version="1.0.0",
    description="Heuristic credit-card pre-approval. Soft-pull prequalification "
                "only; no underwriting. Outputs are directional, not guarantees.",
    # Hide interactive docs in production.
    docs_url=None if settings.is_prod else "/docs",
    redoc_url=None if settings.is_prod else "/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    max_age=600,
)


# --- Middleware: request id + timing + security headers --------------------
@app.middleware("http")
async def observability(request: Request, call_next):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4())[:8])
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error req=%s %s %s", request_id,
                      request.method, request.url.path)
        raise
    elapsed_ms = (time.perf_counter() - start) * 1000
    log.info("req=%s %s %s -> %s %.1fms", request_id, request.method,
             request.url.path, response.status_code, elapsed_ms)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


# --- Uniform error envelopes -----------------------------------------------
@app.exception_handler(ConsentError)
async def consent_error_handler(request: Request, exc: ConsentError):
    return JSONResponse(status_code=403, content={"error": "consent_required",
                                                  "detail": str(exc)})


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=422, content={"error": "invalid_request",
                                                  "detail": str(exc)})


@app.exception_handler(FeedbackError)
async def feedback_error_handler(request: Request, exc: FeedbackError):
    return JSONResponse(status_code=422, content={"error": "invalid_feedback",
                                                  "detail": str(exc)})


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    # Never leak internals in prod.
    detail = "Internal server error" if settings.is_prod else repr(exc)
    return JSONResponse(status_code=500, content={"error": "internal", "detail": detail})


# --- Frontend ---------------------------------------------------------------
@app.get("/", include_in_schema=False)
def frontend():
    index = WEB_DIR / "index.html"
    if not index.exists():
        return JSONResponse(
            status_code=404,
            content={"error": "frontend_missing",
                     "detail": "web/index.html not found; API is still available under /v1."},
        )
    return FileResponse(index)


# --- Health / readiness -----------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict:
    # Readiness includes config that must be valid to serve traffic.
    return {
        "status": "ready",
        "provider": settings.credit_provider,
        "cards_loaded": len(CARD_MATRIX),
        "env": settings.app_env,
    }


# --- Public catalog ---------------------------------------------------------
@app.get("/v1/cards")
def list_cards() -> list[dict]:
    return [c.to_dict() for c in CARD_MATRIX]


# --- Direct match (manual numbers, no consent/pull) -------------------------
@app.post("/v1/match", response_model=list[MatchResult])
def match_direct(
    profile: ProfileIn,
    include_ineligible: bool = False,
    rank_by: str = "approval",
):
    """
    Stateless match against caller-supplied numbers. This is the free tier and
    the fallback; it performs NO external pull and needs NO consent.
    """
    up = UserProfile(**profile.model_dump())
    return match_cards(up, include_ineligible=include_ineligible, rank_by=rank_by)


# --- Consent ----------------------------------------------------------------
@app.post("/v1/consent", response_model=ConsentOut)
def grant_consent(body: ConsentIn, request: Request) -> ConsentOut:
    """
    Record the consumer's authorization for a soft-pull prequalification.
    Must be called (and the returned consent_id used) before /v1/match-from-report.
    """
    if not body.agreed:
        raise ConsentError("Consumer must agree to the disclosure to proceed.")
    record = consent_store.grant(
        consumer_token=body.consumer_token,
        purpose=body.purpose,
        ip=request.client.host if request.client else None,
    )
    return ConsentOut(
        consent_id=record.consent_id,
        purpose=record.purpose,
        disclosure_version=record.disclosure_version,
        expires_at=record.expires_at,
    )


# --- Match from a (consented) credit report ---------------------------------
@app.post("/v1/match-from-report", response_model=ReportMatchOut)
def match_from_report(body: ReportMatchIn) -> ReportMatchOut:
    """
    The real pre-approval flow:
      consent -> provider fetches a normalized CreditReport -> map to profile
      -> run engine -> write audit entry -> return matches + report metadata.

    The active provider is set by CREDIT_PROVIDER (manual | bureau_mock | bureau).
    """
    consent = consent_store.require(body.consent_id, purpose="prequalification")

    provider = get_provider(settings.credit_provider)
    applicant = ApplicantRef(
        token=body.consumer_token,
        manual_values=body.manual_profile.model_dump() if body.manual_profile else None,
    )

    report = provider.fetch(applicant, consent)

    audit.log(
        event="credit_pull",
        provider=report.source,
        inquiry_type=report.inquiry_type,
        consent=consent,
        report_id=report.report_id,
        detail={"rank_by": body.rank_by},
    )

    matches = match_cards(
        report.to_profile(),
        include_ineligible=body.include_ineligible,
        rank_by=body.rank_by,
    )

    return ReportMatchOut(
        report={
            "report_id": report.report_id,
            "source": report.source,
            "inquiry_type": report.inquiry_type,
            "pulled_at": report.pulled_at,
            "extras": report.extras,
        },
        matches=matches,
    )


# --- Feedback loop (calibration) -------------------------------------------
@app.post("/v1/feedback", response_model=FeedbackOut)
def submit_feedback(body: FeedbackIn) -> FeedbackOut:
    """
    Record what actually happened when a user applied for a card. This is the
    ground-truth signal used to calibrate the engine's weights over time.
    """
    if body.card_id not in _VALID_CARD_IDS:
        raise ValueError(f"Unknown card_id {body.card_id!r}")
    rec = feedback_store.record(
        consumer_token=body.consumer_token,
        card_id=body.card_id,
        outcome=body.outcome,
        score=body.score,
        utilization=body.utilization,
        inquiries=body.inquiries,
        new_accounts_24mo=body.new_accounts_24mo,
        report_id=body.report_id,
        approval_probability_shown=body.approval_probability_shown,
        fit_score_shown=body.fit_score_shown,
        notes=body.notes,
    )
    return FeedbackOut(feedback_id=rec.feedback_id)


@app.get("/v1/feedback/stats", response_model=list[CardStat])
def feedback_stats() -> list[dict]:
    """
    Per-card realized approval rates from collected feedback.

    NOTE: in production this exposes aggregate business data — put it behind
    authentication (admin token / internal network) before going live.
    """
    return feedback_store.stats()
