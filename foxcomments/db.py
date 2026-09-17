"""Postgres storage and the article scrape queue."""

import logging
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .discovery import Article

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Ready = never tried, or failed on an earlier run and still has attempts left.
_READY_WHERE = """
    status = 'pending'
    OR (status = 'failed' AND attempts < %(max_attempts)s AND last_attempt_at < %(run_started)s)
"""


class Database:
    def __init__(self, url: str):
        self.conn = psycopg.connect(url, row_factory=dict_row, autocommit=True, connect_timeout=10)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def init_schema(self) -> None:
        self.conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    def now(self) -> datetime:
        return self.conn.execute("SELECT now() AS now").fetchone()["now"]

    # --- runs --------------------------------------------------------------

    def start_run(self, kind: str) -> int:
        return self.conn.execute(
            "INSERT INTO runs (kind) VALUES (%s) RETURNING id", (kind,)
        ).fetchone()["id"]

    def finish_run(self, run_id: int, stats: dict, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = now(), stats = %s, error = %s WHERE id = %s",
            (Jsonb(stats), error, run_id),
        )

    # --- queue -------------------------------------------------------------

    def known_urls(self, urls: list[str]) -> set[str]:
        if not urls:
            return set()
        rows = self.conn.execute("SELECT url FROM articles WHERE url = ANY(%s)", (urls,)).fetchall()
        return {r["url"] for r in rows}

    def enqueue(self, articles: list[Article]) -> int:
        """Insert new articles as pending. Returns how many were actually new."""
        inserted = 0
        with self.conn.transaction():
            for a in articles:
                row = self.conn.execute(
                    """
                    INSERT INTO articles (url, title, published_at, source, matched_keywords)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (url) DO NOTHING
                    RETURNING id
                    """,
                    (a.url, a.title, a.published, a.source, a.matched_keywords),
                ).fetchone()
                inserted += row is not None
        return inserted

    def reset_stale(self, older_than_minutes: int = 120) -> int:
        """Put back articles stuck in_progress (e.g. the process was killed mid-scrape)."""
        cur = self.conn.execute(
            """
            UPDATE articles SET status = 'pending', attempts = GREATEST(attempts - 1, 0)
            WHERE status = 'in_progress' AND last_attempt_at < now() - make_interval(mins => %s)
            """,
            (older_than_minutes,),
        )
        return cur.rowcount

    def claim_next(self, run_started: datetime, max_attempts: int) -> dict | None:
        """Atomically take the oldest ready article and mark it in_progress."""
        return self.conn.execute(
            f"""
            UPDATE articles SET status = 'in_progress', attempts = attempts + 1, last_attempt_at = now()
            WHERE id = (
                SELECT id FROM articles
                WHERE {_READY_WHERE}
                ORDER BY discovered_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, url, title, attempts
            """,
            {"run_started": run_started, "max_attempts": max_attempts},
        ).fetchone()

    def count_ready(self, run_started: datetime, max_attempts: int) -> int:
        return self.conn.execute(
            f"SELECT count(*) AS n FROM articles WHERE {_READY_WHERE}",
            {"run_started": run_started, "max_attempts": max_attempts},
        ).fetchone()["n"]

    def release(self, article_id: int) -> None:
        """Undo a claim without counting it as an attempt (e.g. on Ctrl+C)."""
        self.conn.execute(
            "UPDATE articles SET status = 'pending', attempts = GREATEST(attempts - 1, 0) WHERE id = %s",
            (article_id,),
        )

    def mark_failed(self, article_id: int, error: str) -> None:
        self.conn.execute(
            "UPDATE articles SET status = 'failed', last_error = %s WHERE id = %s", (error, article_id)
        )

    def mark_no_comments(self, article_id: int, error: str) -> None:
        self.conn.execute(
            "UPDATE articles SET status = 'no_comments', last_error = %s WHERE id = %s", (error, article_id)
        )

    # --- results -----------------------------------------------------------

    def save_scrape(self, article_id: int, result: dict) -> None:
        """Store a scrape_article() result and mark the article done, in one transaction."""
        flat = []
        for top in result["comments"]:
            flat.append(top)
            flat.extend(top["replies"])

        authors = {}
        for c in flat:
            a = c["author"]
            if a["id"]:
                authors[a["id"]] = (a["id"], a["username"], a["display_name"])

        with self.conn.transaction(), self.conn.cursor() as cur:
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
                    edited, deleted, pinned, score, reaction_total, reactions, images, videos
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    body = EXCLUDED.body, updated_at = EXCLUDED.updated_at, edited = EXCLUDED.edited,
                    deleted = EXCLUDED.deleted, pinned = EXCLUDED.pinned, score = EXCLUDED.score,
                    reaction_total = EXCLUDED.reaction_total, reactions = EXCLUDED.reactions,
                    images = EXCLUDED.images, videos = EXCLUDED.videos, scraped_at = now()
                """,
                [
                    (
                        c["id"], article_id, c["parent_comment_id"], c["author"]["id"], c["body"],
                        c["created"], c["updated"], c["edited"], c["deleted"], c["pinned"], c["score"],
                        c.get("reaction_total", 0), Jsonb(c.get("reactions", {})),
                        Jsonb(c["images"]), Jsonb(c["videos"]),
                    )
                    for c in flat
                ],
            )
            cur.execute(
                """
                UPDATE articles SET
                    status = 'done', last_error = NULL, scraped_at = now(),
                    comment_thread_id = %s, title = COALESCE(%s, title),
                    top_level_count = %s, reply_count = %s
                WHERE id = %s
                """,
                (
                    result["article"]["id"], result["article"]["title"],
                    result["counts"]["top_level_comments"], result["counts"]["replies"], article_id,
                ),
            )

    # --- reporting ---------------------------------------------------------

    def queue_summary(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, count(*) AS n FROM articles GROUP BY status ORDER BY status"
        ).fetchall()
        totals = self.conn.execute(
            "SELECT (SELECT count(*) FROM comments) AS comments, (SELECT count(*) FROM authors) AS authors"
        ).fetchone()
        return {"articles": {r["status"]: r["n"] for r in rows}, **totals}

    def recent_articles(self, limit: int = 15) -> list[dict]:
        return self.conn.execute(
            """
            SELECT status, attempts, discovered_at, scraped_at, top_level_count, reply_count, title, last_error
            FROM articles ORDER BY discovered_at DESC, id DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()

    def recent_runs(self, limit: int = 5) -> list[dict]:
        return self.conn.execute(
            "SELECT id, kind, started_at, finished_at, stats, error FROM runs ORDER BY id DESC LIMIT %s",
            (limit,),
        ).fetchall()
