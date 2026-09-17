"""Integration tests against a real Postgres (the foxcomments_test database)."""

from datetime import datetime, timedelta, timezone

from foxcomments.discovery import Article


def art(slug, title=None, **kw) -> Article:
    return Article(url=f"https://www.foxnews.com/politics/{slug}", title=title or f"Trump {slug}",
                   source="feed:politics", matched_keywords=["Trump"], **kw)


def scrape_result(embed="93796465-e099-586d-a059-779807a18d27", title="Scraped title", comments=None):
    comments = comments if comments is not None else [
        {
            "id": "c1", "body": "top", "created": "2026-09-16T12:00:00.000Z", "updated": "2026-09-16T12:00:01.000Z",
            "edited": False, "deleted": False, "pinned": False, "score": 1, "parent_comment_id": None,
            "author": {"id": "u1", "username": "alice", "display_name": "Alice"},
            "images": [], "videos": [], "reaction_total": 3, "reactions": {"Agree": 2, "Disagree": 1},
            "replies": [{
                "id": "r1", "body": "reply", "created": "2026-09-16T12:05:00.000Z", "updated": None,
                "edited": True, "deleted": False, "pinned": False, "score": 0, "parent_comment_id": "c1",
                "author": {"id": None, "username": None, "display_name": None},
                "images": [{"url": "x"}], "videos": [], "reaction_total": 0, "reactions": {},
            }],
        },
    ]
    return {
        "article": {"id": embed, "url": "u", "title": title},
        "counts": {"top_level_comments": len(comments), "replies": sum(len(c["replies"]) for c in comments)},
        "comments": comments,
    }


def status_of(db, article_id):
    return db.conn.execute("SELECT * FROM articles WHERE id = %s", (article_id,)).fetchone()


def test_schema_init_is_idempotent(db):
    db.init_schema()
    db.init_schema()


def test_enqueue_inserts_new_and_ignores_duplicates(db):
    published = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    assert db.enqueue([art("a", published=published), art("b"), art("a")]) == 2
    assert db.enqueue([art("a"), art("c")]) == 1
    assert db.known_urls([art("a").url, art("zzz").url]) == {art("a").url}

    row = db.conn.execute("SELECT * FROM articles WHERE url = %s", (art("a").url,)).fetchone()
    assert row["status"] == "pending"
    assert row["published_at"] == published
    assert row["matched_keywords"] == ["Trump"]


def test_known_urls_empty_input(db):
    assert db.known_urls([]) == set()


def test_claim_takes_oldest_first_and_marks_in_progress(db):
    db.enqueue([art("first")])
    db.enqueue([art("second")])
    run_started = db.now()

    job = db.claim_next(run_started, max_attempts=3)
    assert job["url"].endswith("/first")
    assert job["attempts"] == 1
    assert status_of(db, job["id"])["status"] == "in_progress"
    assert db.count_ready(run_started, 3) == 1

    assert db.claim_next(run_started, 3)["url"].endswith("/second")
    assert db.claim_next(run_started, 3) is None


def test_failed_articles_wait_for_a_later_run_and_respect_max_attempts(db):
    db.enqueue([art("flaky")])

    run1 = db.now()
    job = db.claim_next(run1, max_attempts=2)
    db.mark_failed(job["id"], "boom")
    assert db.claim_next(run1, 2) is None, "must not retry within the same run"

    run2 = db.now()
    job = db.claim_next(run2, 2)
    assert job is not None and job["attempts"] == 2
    db.mark_failed(job["id"], "boom again")

    run3 = db.now()
    assert db.count_ready(run3, 2) == 0, "out of attempts"
    assert status_of(db, job["id"])["last_error"] == "boom again"


def test_no_comments_is_terminal(db):
    db.enqueue([art("disabled")])
    job = db.claim_next(db.now(), 3)
    db.mark_no_comments(job["id"], "comments disabled")
    assert db.count_ready(db.now(), 3) == 0
    assert status_of(db, job["id"])["status"] == "no_comments"


def test_release_returns_article_without_using_an_attempt(db):
    db.enqueue([art("interrupted")])
    job = db.claim_next(db.now(), 3)
    db.release(job["id"])
    row = status_of(db, job["id"])
    assert (row["status"], row["attempts"]) == ("pending", 0)


def test_reset_stale_only_touches_old_in_progress(db):
    db.enqueue([art("old"), art("fresh")])
    run = db.now()
    old, fresh = db.claim_next(run, 3), db.claim_next(run, 3)
    db.conn.execute("UPDATE articles SET last_attempt_at = now() - interval '3 hours' WHERE id = %s", (old["id"],))

    assert db.reset_stale(older_than_minutes=120) == 1
    assert (status_of(db, old["id"])["status"], status_of(db, old["id"])["attempts"]) == ("pending", 0)
    assert status_of(db, fresh["id"])["status"] == "in_progress"


def test_save_scrape_stores_comments_authors_and_marks_done(db):
    db.enqueue([art("story", title="Feed title")])
    job = db.claim_next(db.now(), 3)
    db.save_scrape(job["id"], scrape_result())

    row = status_of(db, job["id"])
    assert row["status"] == "done"
    assert (row["top_level_count"], row["reply_count"]) == (1, 1)
    assert str(row["comment_thread_id"]) == "93796465-e099-586d-a059-779807a18d27"
    assert row["title"] == "Scraped title"
    assert row["scraped_at"] is not None

    comments = {c["id"]: c for c in db.conn.execute("SELECT * FROM comments").fetchall()}
    assert set(comments) == {"c1", "r1"}
    c1, r1 = comments["c1"], comments["r1"]
    assert (c1["agree_count"], c1["disagree_count"], c1["reaction_total"]) == (2, 1, 3)
    assert c1["created_at"] == datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
    assert c1["author_id"] == "u1"
    assert (r1["parent_comment_id"], r1["author_id"], r1["edited"]) == ("c1", None, True)
    assert r1["images"] == [{"url": "x"}]
    assert (r1["agree_count"], r1["disagree_count"]) == (0, 0)

    author = db.conn.execute("SELECT * FROM authors").fetchone()
    assert (author["id"], author["username"], author["display_name"]) == ("u1", "alice", "Alice")


def test_save_scrape_keeps_feed_title_when_page_title_missing(db):
    db.enqueue([art("story", title="Feed title")])
    job = db.claim_next(db.now(), 3)
    db.save_scrape(job["id"], scrape_result(title=None, comments=[]))
    assert status_of(db, job["id"])["title"] == "Feed title"


def test_save_scrape_upserts_and_never_blanks_known_author_names(db):
    db.enqueue([art("story")])
    job = db.claim_next(db.now(), 3)
    db.save_scrape(job["id"], scrape_result())

    again = scrape_result()
    again["comments"][0]["body"] = "edited body"
    again["comments"][0]["reactions"] = {"Agree": 10}
    again["comments"][0]["author"] = {"id": "u1", "username": None, "display_name": None}
    db.save_scrape(job["id"], again)

    c1 = db.conn.execute("SELECT * FROM comments WHERE id = 'c1'").fetchone()
    assert (c1["body"], c1["agree_count"]) == ("edited body", 10)
    assert db.conn.execute("SELECT count(*) AS n FROM comments").fetchone()["n"] == 2
    assert db.conn.execute("SELECT username FROM authors WHERE id = 'u1'").fetchone()["username"] == "alice"


def test_save_scrape_is_atomic(db):
    db.enqueue([art("story")])
    job = db.claim_next(db.now(), 3)
    bad = scrape_result()
    bad["comments"][0]["replies"][0]["created"] = "not-a-timestamp"
    try:
        db.save_scrape(job["id"], bad)
    except Exception:
        pass
    else:
        raise AssertionError("expected a database error")
    assert db.conn.execute("SELECT count(*) AS n FROM comments").fetchone()["n"] == 0
    assert db.conn.execute("SELECT count(*) AS n FROM authors").fetchone()["n"] == 0
    assert status_of(db, job["id"])["status"] == "in_progress"


def test_runs_and_reporting(db):
    run_id = db.start_run("daily")
    db.finish_run(run_id, {"work": {"done": 2}})
    db.enqueue([art("a"), art("b")])
    db.claim_next(db.now() + timedelta(seconds=1), 3)

    assert db.recent_runs()[0]["stats"] == {"work": {"done": 2}}
    summary = db.queue_summary()
    assert summary["articles"] == {"in_progress": 1, "pending": 1}
    assert (summary["comments"], summary["authors"]) == (0, 0)
    assert len(db.recent_articles()) == 2
