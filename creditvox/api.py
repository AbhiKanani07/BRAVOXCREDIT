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

from fastapi import FastAPI, Request, Header, HTTPException, Response, Cookie
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

from .config import settings
from .engine import CARD_MATRIX, UserProfile, match_cards, near_misses
from .audit import AuditLogger, ConsentStore, ConsentError
from .feedback import FeedbackStore, FeedbackError
from .auth import AuthStore, AuthError
from .emailer import EmailOutbox
from .storage import init_db
from .providers import ApplicantRef, get_provider
from .schemas import (
    ConsentIn, ConsentOut, MatchResult, ProfileIn,
    ReportMatchIn, ReportMatchOut, FeedbackIn, FeedbackOut, CardStat,
    RegisterIn, LoginIn, UserOut, HistoryItem, DashboardOut,
)
from fastapi.responses import HTMLResponse

SESSION_COOKIE = "cvx_session"

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
auth_store = AuthStore(settings.db_path, session_ttl_days=settings.session_ttl_days)
email_outbox = EmailOutbox(settings.db_path, provider=settings.email_provider)
_VALID_CARD_IDS = {c.id for c in CARD_MATRIX}


def _current_user(token: str | None) -> dict | None:
    """Optional auth: returns the logged-in user or None. Never raises."""
    try:
        return auth_store.user_for_session(token)
    except Exception:
        return None


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE, value=token, httponly=True,
        secure=settings.is_prod, samesite="lax",
        max_age=settings.session_ttl_days * 86400, path="/",
    )

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


@app.exception_handler(AuthError)
async def auth_error_handler(request: Request, exc: AuthError):
    return JSONResponse(status_code=400, content={"error": "auth_error",
                                                  "detail": str(exc)})


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    # Never leak internals in prod.
    detail = "Internal server error" if settings.is_prod else repr(exc)
    return JSONResponse(status_code=500, content={"error": "internal", "detail": detail})


# --- Static assets (logo, images, etc. served from the web/ folder) --------
from fastapi.staticfiles import StaticFiles
if WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIR)), name="assets")


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
    cvx_session: str | None = Cookie(default=None),
):
    """
    Stateless match against caller-supplied numbers. This is the free tier and
    the fallback; it performs NO external pull and needs NO consent. If the
    caller is logged in, the check is saved to their history.
    """
    up = UserProfile(**profile.model_dump())
    results = match_cards(up, include_ineligible=include_ineligible, rank_by=rank_by)
    user = _current_user(cvx_session)
    if user:
        try:
            auth_store.save_check(user["user_id"], profile.model_dump(),
                                  rank_by, "manual", results)
        except Exception:
            log.exception("failed to save check history")
    return results


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
def match_from_report(
    body: ReportMatchIn,
    cvx_session: str | None = Cookie(default=None),
) -> ReportMatchOut:
    """
    The real pre-approval flow:
      consent -> provider fetches a normalized CreditReport -> map to profile
      -> run engine -> write audit entry -> return matches + report metadata.

    The active provider is set by CREDIT_PROVIDER (manual | bureau_mock | bureau).
    """
    user = _current_user(cvx_session)
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

    if user:
        try:
            auth_store.save_check(user["user_id"], report.to_profile().to_dict(),
                                  body.rank_by, report.source, matches)
        except Exception:
            log.exception("failed to save check history")

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


def _require_admin(x_admin_token: str | None) -> None:
    """Gate admin-only endpoints. Fails closed: if no ADMIN_TOKEN is configured,
    the endpoint is disabled entirely rather than left open."""
    if not settings.admin_token:
        raise HTTPException(status_code=503,
                            detail="Admin endpoints disabled: ADMIN_TOKEN is not set.")
    # constant-time compare to avoid leaking the token via timing
    import secrets
    if not x_admin_token or not secrets.compare_digest(x_admin_token, settings.admin_token):
        raise HTTPException(status_code=401, detail="Invalid or missing admin token.")


@app.get("/v1/feedback/stats", response_model=list[CardStat])
def feedback_stats(x_admin_token: str | None = Header(default=None)) -> list[dict]:
    """
    Per-card realized approval rates from collected feedback. ADMIN ONLY.

    Requires an `X-Admin-Token` header matching the ADMIN_TOKEN env var. If
    ADMIN_TOKEN is unset, this endpoint returns 503 (disabled) so it can never
    be left public by accident.
    """
    _require_admin(x_admin_token)
    return feedback_store.stats()


# --- Authentication (optional; enables saved history) ----------------------
@app.post("/v1/auth/register", response_model=UserOut)
def register(body: RegisterIn, response: Response) -> UserOut:
    user = auth_store.register(body.email, body.password)
    token = auth_store.create_session(user["user_id"])
    _set_session_cookie(response, token)
    try:
        email_outbox.enqueue_drip(user["user_id"])  # dormant until a provider is wired
    except Exception:
        log.exception("failed to enqueue welcome drip")
    return UserOut(**user)


@app.post("/v1/auth/login", response_model=UserOut)
def login(body: LoginIn, response: Response) -> UserOut:
    user = auth_store.authenticate(body.email, body.password)
    token = auth_store.create_session(user["user_id"])
    _set_session_cookie(response, token)
    return UserOut(**user)


@app.post("/v1/auth/logout")
def logout(response: Response, cvx_session: str | None = Cookie(default=None)) -> dict:
    auth_store.delete_session(cvx_session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/v1/auth/me", response_model=UserOut)
def me(cvx_session: str | None = Cookie(default=None)) -> UserOut:
    user = _current_user(cvx_session)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in.")
    return UserOut(**user)


@app.get("/v1/history", response_model=list[HistoryItem])
def history(cvx_session: str | None = Cookie(default=None)) -> list[dict]:
    user = _current_user(cvx_session)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in.")
    return auth_store.history(user["user_id"])


# --- Dashboard (logged-in home: history, trends, near-miss nudges) ---------
@app.get("/v1/dashboard", response_model=DashboardOut)
def dashboard(cvx_session: str | None = Cookie(default=None)) -> DashboardOut:
    user = _current_user(cvx_session)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in.")
    uid = user["user_id"]
    hist = auth_store.history(uid)
    latest_profile = auth_store.latest_check(uid)
    nudges = []
    if latest_profile:
        up = UserProfile(
            score=latest_profile["score"] or 300,
            utilization=latest_profile["utilization"] or 0.0,
            inquiries=latest_profile["inquiries"] or 0,
            new_accounts_24mo=latest_profile.get("new_accounts_24mo"),
        )
        nudges = near_misses(up)
    return DashboardOut(
        email=user["email"],
        checks_run=auth_store.count_checks(uid),
        history=hist,
        trend=auth_store.trend(uid),
        near_misses=nudges,
        latest=hist[0] if hist else None,
    )


# --- One-click email unsubscribe (public; CAN-SPAM) ------------------------
@app.get("/unsubscribe", response_class=HTMLResponse, include_in_schema=False)
def unsubscribe(token: str = "") -> HTMLResponse:
    ok = auth_store.unsubscribe(token)
    msg = ("You've been unsubscribed from CreditVox emails."
           if ok else "This unsubscribe link is invalid or already used.")
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8>"
        f"<body style='font-family:system-ui;background:#0A0E17;color:#fff;"
        f"display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
        f"<p style='font-size:18px'>{msg}</p></body>"
    )


# --- Email outbox processing (admin; a cron/worker calls this) -------------
@app.post("/v1/email/process")
def email_process(x_admin_token: str | None = Header(default=None)) -> dict:
    _require_admin(x_admin_token)
    return email_outbox.process_due()
