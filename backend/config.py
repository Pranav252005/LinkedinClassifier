"""Environment-backed settings. Loaded once at import."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"

load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int_env(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


# --- external services -------------------------------------------------------
SERPER_API_KEY = _env("SERPER_API_KEY")
SERPER_BASE_URL = _env("SERPER_BASE_URL", "https://google.serper.dev").rstrip("/")

TYPESAFE_API_KEY = _env("TYPESAFE_API_KEY")
TYPESAFE_BASE_URL = _env("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")

OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
# Cheap, fast, strong-enough models. Override in .env if OpenRouter renames them.
OPENROUTER_TEXT_MODEL = _env("OPENROUTER_TEXT_MODEL", "google/gemini-2.5-flash")
OPENROUTER_VISION_MODEL = _env("OPENROUTER_VISION_MODEL", "google/gemini-2.5-flash")
OPENROUTER_SCORING_MODEL = _env("OPENROUTER_SCORING_MODEL", "google/gemini-2.5-flash")

# Which backend scores candidates. "openrouter" runs on your OpenRouter credits;
# "typesafe" calls Jev's System One endpoint and needs TYPESAFE_API_KEY.
# Jev is not available through OpenRouter — see backend/scoring.py.
SCORING_PROVIDER = (_env("SCORING_PROVIDER", "openrouter") or "openrouter").lower()
# Candidates judged per request. Higher = fewer calls and lower cost, but a long
# prompt the model has to hold in mind at once.
SCORING_BATCH_SIZE = _int_env("SCORING_BATCH_SIZE", 10)

# --- app ---------------------------------------------------------------------
SECRET_KEY = _env("SECRET_KEY", "dev-only-insecure-key-change-me")
# Postgres connection string (Neon, or any Postgres). When set it wins; when
# empty the app falls back to the local SQLite file below.
DATABASE_URL = _env("DATABASE_URL")

# Resolve a relative DB_PATH against the project root, not the process's working
# directory — otherwise the database silently moves depending on where uvicorn
# was launched from (the README says to start it from backend/).
_db_path = Path(_env("DB_PATH") or "data/app.db")
DB_PATH = _db_path if _db_path.is_absolute() else ROOT / _db_path
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
SESSION_COOKIE = "jcs_session"
# Send the session cookie only over HTTPS. Derived from PUBLIC_BASE_URL so a
# production deploy is secure by default; override with COOKIE_SECURE=true/false.
_cookie_secure_env = _env("COOKIE_SECURE").lower()
COOKIE_SECURE = (
    _cookie_secure_env in ("1", "true", "yes")
    if _cookie_secure_env
    else PUBLIC_BASE_URL.startswith("https://")
)
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

# --- plans -------------------------------------------------------------------
# Accounts that are always on Pro without paying — the operator's own logins.
# Declared here rather than written into the database so the grant survives a
# database reset, a re-signup with a different password, and any Stripe event.
COMP_ACCOUNTS = frozenset(
    e.strip().lower() for e in _env("COMP_ACCOUNTS").split(",") if e.strip()
)

FREE_RUNS_PER_MONTH = _int_env("FREE_RUNS_PER_MONTH", 3)
PRO_RUNS_PER_MONTH = _int_env("PRO_RUNS_PER_MONTH", 250)
PRO_PRICE_LABEL = _env("PRO_PRICE_LABEL", "$5/mo")

# --- stripe ------------------------------------------------------------------
STRIPE_SECRET_KEY = _env("STRIPE_SECRET_KEY")
STRIPE_PRICE_ID = _env("STRIPE_PRICE_ID")
STRIPE_WEBHOOK_SECRET = _env("STRIPE_WEBHOOK_SECRET")

MAX_UPLOAD_BYTES = _int_env("MAX_UPLOAD_BYTES", 8 * 1024 * 1024)


def billing_enabled() -> bool:
    return bool(STRIPE_SECRET_KEY and STRIPE_PRICE_ID)


def is_comp_account(email: str) -> bool:
    return (email or "").strip().lower() in COMP_ACCOUNTS


def runs_allowed(plan: str) -> int:
    return PRO_RUNS_PER_MONTH if plan == "pro" else FREE_RUNS_PER_MONTH
