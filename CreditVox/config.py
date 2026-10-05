"""
config.py
=========
Environment-driven settings. Pure stdlib (no pydantic-settings dependency) so
the domain core stays lean. Everything has a safe dev default; override via env
vars (or a .env loaded by your process manager) in production.
"""

from __future__ import annotations

import os


def _csv(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings:
    def __init__(self) -> None:
        # dev | prod. Flips docs exposure and error-detail verbosity.
        self.app_env: str = os.getenv("APP_ENV", "dev").lower()

        # Which credit data provider the /match-from-report flow uses.
        #   manual       -> caller supplies the numbers (no external pull)
        #   bureau_mock  -> deterministic fake bureau report (for building/testing)
        #   bureau       -> real aggregator (stub; raises until wired)
        self.credit_provider: str = os.getenv("CREDIT_PROVIDER", "bureau_mock").lower()

        # Browser origins allowed to call the API. Your frontend domain lives here.
        self.cors_origins: list[str] = _csv(
            "CORS_ORIGINS",
            "https://bravoxcredit.com,"
            "https://www.bravoxcredit.com,"
            "http://localhost:8000,"
            "http://127.0.0.1:8000,"
            "http://localhost:3000,"
            "http://127.0.0.1:3000",
        )

        # SQLite database file for consent + audit persistence. Survives restarts.
        self.db_path: str = os.getenv("DB_PATH", "creditvox.db")

        # If set (postgres:// or postgresql://), the app uses Postgres instead of
        # SQLite and db_path is ignored. This is the only switch needed to scale
        # past a single worker.
        self.database_url: str = os.getenv("DATABASE_URL", "").strip()

        # How long a consumer's consent stays valid before they must re-consent.
        self.consent_ttl_minutes: int = int(os.getenv("CONSENT_TTL_MINUTES", "30"))

        # Version string of the consumer disclosure text they agree to. Bump on
        # any wording change; it's recorded per consent for audit.
        self.disclosure_version: str = os.getenv("DISCLOSURE_VERSION", "2026-01-v1")

        self.log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"


settings = Settings()
