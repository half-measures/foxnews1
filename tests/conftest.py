import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # make `fakes` importable

from fakes import FakeClock, FakeSession  # noqa: E402
from foxcomments import client as client_module  # noqa: E402
from foxcomments import rate_limiter as rate_limiter_module  # noqa: E402
from foxcomments.client import FoxCommentsClient  # noqa: E402
from foxcomments.rate_limiter import RateLimiter  # noqa: E402

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://foxcomments:foxcomments@127.0.0.1:5432/foxcomments_test"
)


@pytest.fixture
def clock(monkeypatch):
    """Fake time for the rate limiter and client, so retries and throttling run instantly."""
    fake = FakeClock()
    monkeypatch.setattr(rate_limiter_module, "time", fake)
    monkeypatch.setattr(client_module, "time", fake)
    return fake


@pytest.fixture
def session():
    return FakeSession()


@pytest.fixture
def client(clock, session):
    c = FoxCommentsClient(RateLimiter(rate=1000, burst=1000))
    c.session = session
    return c


# --- database ------------------------------------------------------------

def _ensure_test_database(url: str) -> None:
    import psycopg
    from psycopg import sql

    dbname = url.rsplit("/", 1)[-1]
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin_url, autocommit=True, connect_timeout=3) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))


@pytest.fixture(scope="session")
def database_url():
    if not TEST_DATABASE_URL.rsplit("/", 1)[-1].endswith("_test"):
        pytest.exit("TEST_DATABASE_URL must point at a database whose name ends in _test", returncode=2)
    try:
        _ensure_test_database(TEST_DATABASE_URL)
    except Exception as exc:
        pytest.skip(f"Postgres not available ({exc.__class__.__name__}); run `docker compose up -d`")
    return TEST_DATABASE_URL


@pytest.fixture
def db(database_url):
    from foxcomments.db import Database

    database = Database(database_url)
    database.init_schema()
    database.conn.execute("TRUNCATE comments, authors, articles, runs RESTART IDENTITY")
    yield database
    database.close()
