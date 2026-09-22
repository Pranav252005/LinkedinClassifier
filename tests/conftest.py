"""Tests run on a throwaway SQLite file, never the DATABASE_URL in .env.

The environment is set before any backend module is imported: config.py loads
.env without overriding variables that already exist, so an empty
DATABASE_URL here wins over the real one.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="leadclassifier-tests-")
# Set TEST_DATABASE_URL to a disposable Postgres to run the same suite there;
# every table in it is dropped between tests.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "")
os.environ["DB_PATH"] = str(Path(_tmp) / "test.db")
os.environ["SECRET_KEY"] = "test-secret"
os.environ["COMP_ACCOUNTS"] = "operator@example.com"
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
for key in ("SERPER_API_KEY", "OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "STRIPE_SECRET_KEY", "POLL_TOKEN"):
    os.environ[key] = ""
os.environ["SIGNUP_LIMIT"] = "0"
os.environ["LOGIN_LIMIT"] = "0"
os.environ["RUN_LIMIT"] = "0"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import pytest  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def fresh_db():
    import db
    if db.IS_POSTGRES:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    else:
        path = Path(os.environ["DB_PATH"])
        if path.exists():
            path.unlink()
    db.init_db()
    import weights
    weights._CACHE.clear()
    yield
