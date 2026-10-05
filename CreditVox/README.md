# CreditVox

Credit-card pre-approval stack. A consumer enters or "pulls" their credit profile
and sees which cards they'd likely be approved for, with approval odds. Soft-pull
prequalification only — never underwriting, never a hit to the score.

> Estimates, not guarantees. The card thresholds are modeled from public
> underwriting patterns, not real bureau data. CreditVox is not a lender or a
> credit reporting agency. Put this in your Terms of Service.

## What's in the box
```
creditvox/            # backend (a Python package)
  engine.py           #   rules engine + card matrix           (pure stdlib)
  providers.py        #   data sources: Manual | MockBureau | Real(stub)   (pure stdlib)
  audit.py            #   consent + audit trail                (pure stdlib)
  feedback.py         #   outcome logging (calibration loop)   (pure stdlib)
  storage.py          #   DB layer: SQLite or Postgres         (pure stdlib*)
  config.py           #   env-driven settings                  (pure stdlib)
  schemas.py          #   pydantic request/response models
  api.py              #   FastAPI app: CORS, logging, errors, endpoints, serves the UI
web/
  index.html          # frontend (single self-contained file, no build step)
tests/                # pytest suite (engine, storage/audit/providers, feedback, api)
requirements.txt          # runtime deps (web layer)
requirements-dev.txt      # + pytest, httpx
requirements-postgres.txt # + psycopg (only if you use Postgres)
Dockerfile / Procfile / run.py / .env.example / .gitignore / .dockerignore
```
`*` storage.py is stdlib for SQLite; Postgres pulls in psycopg only when selected.
The whole domain core is dependency-free — only the HTTP boundary needs fastapi/pydantic.

## Architecture
```
consent -> provider.fetch() -> CreditReport -> UserProfile -> engine -> audit log -> matches
                                                                          \-> user reports outcome -> feedback -> calibration
```

## Run it (local, SQLite — zero extra setup)
From the folder that CONTAINS the `creditvox/` package:
```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
python run.py                 # or: uvicorn creditvox.api:app --reload
```
Open http://127.0.0.1:8000 — the frontend is served there. Dev API docs at /docs.
Defaults to CREDIT_PROVIDER=bureau_mock, so "Run a demo check" works immediately.

## Run the tests
```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
```

## Endpoints
| Method | Path | Purpose |
|--------|------|---------|
| GET  | /                       | the frontend |
| GET  | /health                 | liveness |
| GET  | /ready                  | readiness + active provider + card count |
| GET  | /v1/cards               | raw underwriting matrix |
| POST | /v1/match               | stateless match on supplied numbers (free tier; no pull, no consent) |
| POST | /v1/consent             | record soft-pull consent -> consent_id |
| POST | /v1/match-from-report   | consent-gated: provider pulls a report, engine matches, pull is audited |
| POST | /v1/feedback            | record a real outcome (applied/approved/denied) for calibration |
| GET  | /v1/feedback/stats      | per-card realized approval rates (PROTECT THIS in prod) |

## The feedback / calibration loop
The engine ships with *estimated* thresholds and weights. `/v1/feedback` turns
real usage into ground truth. The frontend already calls it: every eligible
result card shows "Applied for this? Approved / Denied", which POSTs the outcome
plus the numbers that produced it.

Programmatic example:
```bash
curl.exe -X POST http://127.0.0.1:8000/v1/feedback -H "Content-Type: application/json" -d "{\"consumer_token\":\"session-abc123\",\"card_id\":\"discover_it_cashback\",\"outcome\":\"approved\",\"score\":700,\"approval_probability_shown\":88}"
```
Read the signal back:
```bash
curl.exe http://127.0.0.1:8000/v1/feedback/stats
# [{ "card_id": "...", "approved": 12, "denied": 3, "approval_rate": 0.8, ... }]
```
When a card's realized `approval_rate` drifts from the probabilities you're
showing, that's your cue to retune `WEIGHT_*`, `SCORE_RUNWAY`, and
`PENALTY_PER_TIER` in `engine.py`. Note: `/v1/feedback/stats` exposes aggregate
business data — put it behind an admin token or internal network before launch.

## Switching to Postgres (scale past one worker)
SQLite + a single worker is fine for launch. When you need more, it's one env var
and one extra dependency — no code change:
```bash
python3 -m pip install -r requirements-postgres.txt
export DATABASE_URL="postgresql://user:pass@host:5432/creditvox"   # set in your host's dashboard
```
`storage.py` detects the URL and uses Postgres; `DB_PATH` is ignored. Tables are
created automatically on startup. With Postgres you can safely run multiple
uvicorn workers (bump the Dockerfile CMD with `--workers N`).

## Deploy to bravoxcredit.com
The frontend is served by the API, so it's one deployable unit.

**One container:**
```bash
docker build -t creditvox .
docker run -p 8000:8000 --env-file .env creditvox
```
Point bravoxcredit.com at the host, put it behind HTTPS. Because UI and API share
an origin, CORS isn't in play for the bundled case; `CORS_ORIGINS` still covers a
separately-hosted frontend.

**PaaS (Railway/Render/Fly):** they read the `Procfile`. Set env vars from
`.env.example` in the dashboard, set `APP_ENV=prod`, attach a Postgres add-on and
set `DATABASE_URL`.

### Before real users
- HTTPS everywhere.
- Use Postgres (above), not SQLite, for anything beyond a demo.
- Protect `/v1/feedback/stats`.
- Write the real consent disclosure text + a privacy policy / ToS.

## Going live with real bureau data
1. Sign with a soft-pull aggregator (Array / iSoftpull / CRS / Soft Pull Solutions) —
   they hold the bureau relationships and handle FCRA vetting.
2. Implement `RealBureauProvider.fetch()` in `providers.py`: POST the consented
   applicant to their sandbox, map their JSON into `CreditReport`, keep
   `inquiry_type="soft"`.
3. Set `CREDIT_PROVIDER=bureau`.
Nothing else changes — engine, consent gate, audit trail, feedback loop, and
frontend all stay put.
