"""Postgres storage and the article scrape queue.

The connection is opened on demand and can be dropped between units of work
(see `disconnect`). A scrape run has minutes-long gaps between articles, and
holding an idle connection open across them is what gets it silently killed by
a firewall, a NAT table or the server's own idle timeout. Every call also
reconnects once if the connection went away between uses.
"""

import logging
import time
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .discovery import Article

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Ready = old enough to scrape, and either never tried or failed on an earlier run with attempts left;
# or already scraped and due a re-scrape.
_READY_WHERE = """
    (
        scrape_after <= now()
        AND (
            status = 'pending'
            OR (status = 'failed' AND attempts < %(max_attempts)s AND last_attempt_at < %(run_started)s)
        )
    )
    OR (status = 'done' AND rescrape_at <= now())
"""

# A claimed article with a scraped_at is a re-scrape: it goes back to `done` rather than
# `pending` or `failed`, and doesn't use up the attempts meant for its first scrape.
_IS_RESCRAPE = "scraped_at IS NOT NULL"

# Return a claimed article to where it was, without counting the claim as an attempt.
_UNCLAIM = f"""
    status = CASE WHEN {_IS_RESCRAPE} THEN 'done' ELSE 'pending' END,
    attempts = CASE WHEN {_IS_RESCRAPE} THEN attempts ELSE GREATEST(attempts - 1, 0) END
"""


class Database:
    def __init__(self, url: str, connect_attempts: int = 3, connect_timeout: int = 10):
        self.url = url
        self.connect_attempts = connect_attempts
        self.connect_timeout = connect_timeout
        self._conn: psycopg.Connection | None = None

    # --- connection handling -------------------------------------------------

    @property
    def conn(self) -> psycopg.Connection:
        """The live connection, opening one if needed."""
        if self._conn is None or self._conn.closed:
            self._conn = self._connect()
        return self._conn

    def _connect(self) -> psycopg.Connection:
        for attempt in range(1, self.connect_attempts + 1):
            try:
                return psycopg.connect(
                    self.url, row_factory=dict_row, autocommit=True,
                    connect_timeout=self.connect_timeout,
                    # Notice a dead peer instead of blocking forever on a remote host.
                    keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
                )
            except psycopg.OperationalError as exc:
                if attempt == self.connect_attempts:
                    raise
                wait = 2 ** attempt
                log.warning("Database connection failed (%s); retrying in %ss", exc, wait)
                time.sleep(wait)
        raise RuntimeError("unreachable")

    def _run(self, operation):
        """Run `operation(conn)`, reconnecting and retrying once if the connection died.

        Safe to retry: every statement here is either idempotent or wrapped in a transaction.
        """
        reusing_connection = self._conn is not None and not self._conn.closed
        try:
            return operation(self.conn)
        except psycopg.OperationalError as exc:
            if not reusing_connection:
                raise  # opening the connection is what failed; _connect already retried
            log.warning("Database connection lost (%s); reconnecting", exc)
            self.disconnect()
            return operation(self.conn)

    def _execute(self, *args, **kwargs):
        return self._run(lambda conn: conn.execute(*args, **kwargs))

    def disconnect(self) -> None:
        """Drop the connection. The next call opens a fresh one."""
        if self._conn is not None and not self._conn.closed:
            try:
                self._conn.close()
            except psycopg.Error as exc:  # already gone; nothing to salvage
                log.debug("Ignoring error while closing the connection: %s", exc)
        self._conn = None

    close = disconnect

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.disconnect()

    def init_schema(self) -> None:
        self._execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    def now(self) -> datetime:
        return self._execute("SELECT now() AS now").fetchone()["now"]

    # --- runs ----------------------------------------------------------------

    def start_run(self, kind: str) -> int:
        return self._execute("INSERT INTO runs (kind) VALUES (%s) RETURNING id", (kind,)).fetchone()["id"]

    def finish_run(self, run_id: int, stats: dict, error: str | None = None) -> None:
        self._execute(
            "UPDATE runs SET finished_at = now(), stats = %s, error = %s WHERE id = %s",
            (Jsonb(stats), error, run_id),
        )

    # --- queue ---------------------------------------------------------------

    def known_urls(self, urls: list[str]) -> set[str]:
        if not urls:
            return set()
        rows = self._execute("SELECT url FROM articles WHERE url = ANY(%s)", (urls,)).fetchall()
        return {r["url"] for r in rows}

    def enqueue(self, articles: list[Article], min_age_hours: float = 0) -> int:
        """Insert new articles as pending. Returns how many were actually new.

        They are queued immediately (feeds drop articles quickly) but not scraped until
        `min_age_hours` after publication, so comments have time to accumulate.
        """
        def operation(conn):
            inserted = 0
            with conn.transaction():
                for a in articles:
                    row = conn.execute(
                        """
                        INSERT INTO articles (url, title, published_at, source, matched_keywords, scrape_after)
                        VALUES (%(url)s, %(title)s, %(published)s, %(source)s, %(keywords)s,
                                GREATEST(COALESCE(%(published)s::timestamptz, now())
                                         + make_interval(secs => %(min_age_seconds)s), now()))
                        ON CONFLICT (url) DO NOTHING
                        RETURNING id
                        """,
                        {"url": a.url, "title": a.title, "published": a.published, "source": a.source,
                         "keywords": a.matched_keywords, "min_age_seconds": min_age_hours * 3600},
                    ).fetchone()
                    inserted += row is not None
            return inserted

        return self._run(operation)

    def reset_stale(self, older_than_minutes: int = 120) -> int:
        """Put back articles stuck in_progress (e.g. the process was killed mid-scrape)."""
        return self._execute(
            f"""
            UPDATE articles SET {_UNCLAIM}
            WHERE status = 'in_progress' AND last_attempt_at < now() - make_interval(mins => %s)
            """,
            (older_than_minutes,),
        ).rowcount

    def schedule_rescrapes(self, after_hours: list[float]) -> int:
        """Give each done article with no re-scrape pending its next one: the first of
        `after_hours` (counted from publication) that is still in the future.

        Articles past the last one stay unscheduled. Returns how many were scheduled.
        """
        if not after_hours:
            return 0
        return self._execute(
            """
            WITH due AS (
                SELECT id, min(COALESCE(published_at, discovered_at) + make_interval(secs => h * 3600)) AS at
                FROM articles, unnest(%s::float8[]) AS h
                WHERE status = 'done' AND rescrape_at IS NULL
                  AND COALESCE(published_at, discovered_at) + make_interval(secs => h * 3600) > now()
                GROUP BY id
            )
            UPDATE articles SET rescrape_at = due.at FROM due WHERE articles.id = due.id
            """,
            (list(after_hours),),
        ).rowcount

    def claim_next(self, run_started: datetime, max_attempts: int) -> dict | None:
        """Atomically take the oldest ready article and mark it in_progress."""
        return self._execute(
            f"""
            UPDATE articles SET status = 'in_progress', last_attempt_at = now(),
                                attempts = attempts + (NOT ({_IS_RESCRAPE}))::int
            WHERE id = (
                SELECT id FROM articles
                WHERE {_READY_WHERE}
                ORDER BY discovered_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, url, title, attempts, {_IS_RESCRAPE} AS rescrape
            """,
            {"run_started": run_started, "max_attempts": max_attempts},
        ).fetchone()

    def count_ready(self, run_started: datetime, max_attempts: int) -> int:
        return self._execute(
            f"SELECT count(*) AS n FROM articles WHERE {_READY_WHERE}",
            {"run_started": run_started, "max_attempts": max_attempts},
        ).fetchone()["n"]

    def release(self, article_id: int) -> None:
        """Undo a claim without counting it as an attempt (e.g. on Ctrl+C or shutdown)."""
        self._execute(f"UPDATE articles SET {_UNCLAIM} WHERE id = %s", (article_id,))

    def mark_rescrape_failed(self, article_id: int, error: str) -> None:
        """A re-scrape went wrong: keep what the earlier scrape stored and move on to the next
        scheduled re-scrape, if any."""
        self._execute(
            "UPDATE articles SET status = 'done', rescrape_at = NULL, last_error = %s WHERE id = %s",
            (error, article_id),
        )

    def mark_failed(self, article_id: int, error: str) -> None:
        self._execute(
            "UPDATE articles SET status = 'failed', last_error = %s WHERE id = %s", (error, article_id)
        )

    def mark_no_comments(self, article_id: int, error: str) -> None:
        self._execute(
            "UPDATE articles SET status = 'no_comments', last_error = %s WHERE id = %s", (error, article_id)
        )

    # --- results -------------------------------------------------------------

    def save_scrape(self, article_id: int, result: dict) -> int:
        """Store a scrape_article() result and mark the article done, in one transaction.

        Returns how many comments (including replies) weren't stored before, which on a
        re-scrape is what was posted since the last one.
        """
        flat = []
        for top in result["comments"]:
            flat.append(top)
            flat.extend(top["replies"])

        authors = {}
        for c in flat:
            a = c["author"]
            if a["id"]:
                authors[a["id"]] = (a["id"], a["username"], a["display_name"])

        def stored(cur) -> int:
            return cur.execute(
                "SELECT count(*) AS n FROM comments WHERE article_id = %s", (article_id,)
            ).fetchone()["n"]

        def operation(conn):
            with conn.transaction(), conn.cursor() as cur:
                before = stored(cur)
                cur.executemany(
                    """
                    INSERT INTO authors (id, username, display_name) VALUES (%s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        username     = COALESCE(EXCLUDED.username, authors.username),
                        display_name = COALESCE(EXCLUDED.display_name, authors.display_name),
                        last_seen_at = now()
                    """,
                    list(authors.values()),
                )
                cur.executemany(
                    """
                    INSERT INTO comments (
                        id, article_id, parent_comment_id, author_id, body, created_at, updated_at,
                        edited, deleted, pinned, score, reaction_total, reactions, images, videos, raw
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        body = EXCLUDED.body, updated_at = EXCLUDED.updated_at, edited = EXCLUDED.edited,
                        deleted = EXCLUDED.deleted, pinned = EXCLUDED.pinned, score = EXCLUDED.score,
                        reaction_total = EXCLUDED.reaction_total, reactions = EXCLUDED.reactions,
                        images = EXCLUDED.images, videos = EXCLUDED.videos,
                        raw = COALESCE(EXCLUDED.raw, comments.raw), scraped_at = now()
                    """,
                    [
                        (
                            c["id"], article_id, c["parent_comment_id"], c["author"]["id"], c["body"],
                            c["created"], c["updated"], c["edited"], c["deleted"], c["pinned"], c["score"],
                            c.get("reaction_total", 0), Jsonb(c.get("reactions", {})),
                            Jsonb(c["images"]), Jsonb(c["videos"]),
                            Jsonb(c["raw"]) if c.get("raw") is not None else None,
                        )
                        for c in flat
                    ],
                )
                cur.execute(
                    """
                    UPDATE articles SET
                        status = 'done', last_error = NULL, scraped_at = now(), rescrape_at = NULL,
                        comment_thread_id = %s, title = COALESCE(%s, title),
                        top_level_count = %s, reply_count = %s
                    WHERE id = %s
                    """,
                    (
                        result["article"]["id"], result["article"]["title"],
                        result["counts"]["top_level_comments"], result["counts"]["replies"], article_id,
                    ),
                )
                return stored(cur) - before

        return self._run(operation)

    # --- reporting -----------------------------------------------------------

    def queue_summary(self) -> dict:
        rows = self._execute(
            "SELECT status, count(*) AS n FROM articles GROUP BY status ORDER BY status"
        ).fetchall()
        totals = self._execute(
            """
            SELECT (SELECT count(*) FROM comments) AS comments,
                   (SELECT count(*) FROM authors) AS authors,
                   (SELECT count(*) FROM articles WHERE status = 'pending' AND scrape_after > now()) AS waiting,
                   (SELECT count(*) FROM articles WHERE rescrape_at IS NOT NULL) AS rescrapes_scheduled
            """
        ).fetchone()
        return {"articles": {r["status"]: r["n"] for r in rows}, **totals}

    def last_run_at(self, kind: str) -> datetime | None:
        """When a run of this kind last started, successful or not (one attempt per slot)."""
        return self._execute(
            "SELECT max(started_at) AS last FROM runs WHERE kind = %s", (kind,)
        ).fetchone()["last"]

    def recent_articles(self, limit: int = 15) -> list[dict]:
        return self._execute(
            """
            SELECT status, attempts, discovered_at, scrape_after, scraped_at,
                   top_level_count, reply_count, title, last_error
            FROM articles ORDER BY discovered_at DESC, id DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()

    def recent_runs(self, limit: int = 5) -> list[dict]:
        return self._execute(
            "SELECT id, kind, started_at, finished_at, stats, error FROM runs ORDER BY id DESC LIMIT %s",
            (limit,),
        ).fetchall()
