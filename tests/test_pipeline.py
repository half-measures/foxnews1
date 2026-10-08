"""Pipeline tests: real Postgres, fake Fox server, fake clock."""

from datetime import datetime, timedelta, timezone

import pytest

from fakes import article_html, comment, page, user
from foxcomments import pipeline
from foxcomments.config import Config
from foxcomments.discovery import Article


@pytest.fixture
def pipeline_clock(clock, monkeypatch):
    monkeypatch.setattr(pipeline, "time", clock)
    monkeypatch.setattr(pipeline.random, "uniform", lambda a, b: 1.0)  # no jitter
    return clock


@pytest.fixture
def cfg():
    c = Config()
    c.worker.window_minutes = 90
    c.worker.min_gap_seconds = 30
    return c


def fox_article(session, slug, n_comments=2, embed=None):
    url = f"https://www.foxnews.com/politics/{slug}"
    embed = embed or f"00000000-0000-0000-0000-{abs(hash(slug)) % 10**12:012d}"
    session.pages[url] = article_html(embed, title=f"Trump {slug}")
    session.comment_pages[embed] = {
        "0": page([comment(f"{slug}-c{i}", creator="u1") for i in range(n_comments)], users=[user("u1")]),
    }
    return url


def found(url, published=None):
    return Article(url=url, title=f"Trump {url.rsplit('/', 1)[-1]}", published=published,
                   source="feed:politics", matched_keywords=["Trump"])


def statuses(db):
    rows = db.conn.execute("SELECT url, status FROM articles ORDER BY id").fetchall()
    return {r["url"].rsplit("/", 1)[-1]: r["status"] for r in rows}


# --- discover ------------------------------------------------------------------

def test_discover_queues_newest_up_to_cap_and_skips_known(db, client, cfg, monkeypatch):
    def at(day):
        return datetime(2026, 9, day, tzinfo=timezone.utc)

    candidates = [
        found("https://www.foxnews.com/politics/old", at(1)),
        found("https://www.foxnews.com/politics/newest", at(16)),
        found("https://www.foxnews.com/politics/undated", None),
        found("https://www.foxnews.com/politics/middle", at(10)),
    ]
    monkeypatch.setattr(pipeline, "find_articles", lambda *a, **k: list(candidates))
    cfg.discovery.max_new_articles = 2

    stats = pipeline.discover(db, client, cfg)
    assert stats == {"matched": 4, "already_known": 0, "new": 4, "queued": 2}
    assert set(statuses(db)) == {"newest", "middle"}

    stats = pipeline.discover(db, client, cfg)
    assert stats == {"matched": 4, "already_known": 2, "new": 2, "queued": 2}
    assert set(statuses(db)) == {"newest", "middle", "old", "undated"}


def test_discover_dry_run_writes_nothing(db, client, cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "find_articles", lambda *a, **k: [found("https://www.foxnews.com/politics/x")])
    assert pipeline.discover(db, client, cfg, dry_run=True)["queued"] == 0
    assert statuses(db) == {}


def test_discover_zero_cap_means_unlimited(db, client, cfg, monkeypatch):
    urls = [found(f"https://www.foxnews.com/politics/s{i}") for i in range(15)]
    monkeypatch.setattr(pipeline, "find_articles", lambda *a, **k: urls)
    cfg.discovery.max_new_articles = 0
    assert pipeline.discover(db, client, cfg)["queued"] == 15


def test_discover_through_real_feed_parsing(db, client, session, cfg):
    from fakes import rss

    session.feeds["politics"] = rss([
        {"link": "https://www.foxnews.com/politics/trump-one", "title": "Trump one"},
        {"link": "https://www.foxnews.com/politics/other", "title": "Something else"},
        {"link": "https://www.foxnews.com/politics/trump-two", "title": "Trump two",
         "pubDate": "Wed, 16 Sep 2026 19:05:04 -0000"},  # naive-date edge case mixed with aware ones
    ])
    cfg.discovery.feeds = ["politics"]
    assert pipeline.discover(db, client, cfg)["queued"] == 2


def test_discover_queues_fresh_articles_but_work_leaves_them_to_mature(
        db, client, session, cfg, pipeline_clock, monkeypatch):
    now = db.now()
    fresh_url = fox_article(session, "fresh")
    old_url = fox_article(session, "old")
    monkeypatch.setattr(pipeline, "find_articles", lambda *a, **k: [
        found(fresh_url, now - timedelta(hours=2)),
        found(old_url, now - timedelta(days=2)),
    ])
    cfg.discovery.min_article_age_hours = 24

    assert pipeline.discover(db, client, cfg)["queued"] == 2
    assert pipeline.work(db, client, cfg, window_minutes=0) == {"done": 1, "comments": 2, "replies": 0}
    assert statuses(db) == {"fresh": "pending", "old": "done"}

    # Once it has matured, the next run picks it up.
    db.conn.execute("UPDATE articles SET scrape_after = now() WHERE status = 'pending'")
    assert pipeline.work(db, client, cfg, window_minutes=0)["done"] == 1
    assert statuses(db) == {"fresh": "done", "old": "done"}


# --- work --------------------------------------------------------------------

def test_work_scrapes_queue_and_spreads_it_across_window(db, client, session, cfg, pipeline_clock):
    urls = [fox_article(session, s) for s in ("a", "b", "c")]
    db.enqueue([found(u) for u in urls])

    stats = pipeline.work(db, client, cfg)

    assert stats == {"done": 3, "comments": 6, "replies": 0}
    assert statuses(db) == {"a": "done", "b": "done", "c": "done"}
    # 90 min window, 3 articles, scraping takes ~0s on the fake clock:
    # after #1: 5400s left / 2 remaining = 2700s; after #2: 2700s left / 1 remaining = 2700s.
    gaps = [s for s in pipeline_clock.sleeps if s >= 30]
    assert gaps == pytest.approx([2700, 2700])


def test_work_uses_min_gap_once_window_is_used_up(db, client, session, cfg, pipeline_clock):
    for s in ("a", "b", "c"):
        db.enqueue([found(fox_article(session, s))])
    pipeline.work(db, client, cfg, window_minutes=0)
    assert [s for s in pipeline_clock.sleeps if s >= 1] == [30, 30]


def test_work_lets_go_of_the_connection_between_articles(db, client, session, cfg, pipeline_clock):
    """A remote database shouldn't see an idle session across the multi-minute gaps."""
    for slug in ("a", "b"):
        db.enqueue([found(fox_article(session, slug))])

    disconnects = []
    real_disconnect = db.disconnect
    db.disconnect = lambda: (disconnects.append(pipeline_clock.now), real_disconnect())

    pipeline.work(db, client, cfg)

    assert disconnects, "expected the connection to be dropped before sleeping"
    # Each drop happens before a sleep, so the gaps are spent with no connection open.
    assert len(disconnects) == len([s for s in pipeline_clock.sleeps if s >= cfg.worker.min_gap_seconds])


def test_work_with_empty_queue(db, client, cfg, pipeline_clock):
    assert pipeline.work(db, client, cfg) == {}
    assert pipeline_clock.sleeps == []


def test_work_records_failures_and_no_comment_articles_and_continues(db, client, session, cfg, pipeline_clock):
    ok = fox_article(session, "ok")
    disabled = "https://www.foxnews.com/politics/disabled"
    session.pages[disabled] = article_html(None)
    missing = "https://www.foxnews.com/politics/missing"  # 404
    db.enqueue([found(missing), found(disabled), found(ok)])

    stats = pipeline.work(db, client, cfg, window_minutes=0)

    assert stats == {"failed": 1, "no_comments": 1, "done": 1, "comments": 2, "replies": 0}
    assert statuses(db) == {"missing": "failed", "disabled": "no_comments", "ok": "done"}
    err = db.conn.execute("SELECT last_error FROM articles WHERE url = %s", (missing,)).fetchone()["last_error"]
    assert err.startswith("HTTPError: 404")


def test_failed_article_retried_next_run_until_max_attempts(db, client, session, cfg, pipeline_clock):
    url = "https://www.foxnews.com/politics/later"
    db.enqueue([found(url)])
    cfg.worker.max_attempts = 3

    assert pipeline.work(db, client, cfg, window_minutes=0) == {"failed": 1}
    assert pipeline.work(db, client, cfg, window_minutes=0) == {"failed": 1}

    fox_article(session, "later")  # the article starts working on the third run
    assert pipeline.work(db, client, cfg, window_minutes=0)["done"] == 1
    assert statuses(db) == {"later": "done"}


def test_ctrl_c_puts_article_back(db, client, cfg, pipeline_clock, monkeypatch):
    db.enqueue([found("https://www.foxnews.com/politics/x")])

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "scrape_article", interrupted)
    with pytest.raises(KeyboardInterrupt):
        pipeline.work(db, client, cfg)
    row = db.conn.execute("SELECT status, attempts FROM articles").fetchone()
    assert (row["status"], row["attempts"]) == ("pending", 0)


def test_work_recovers_stale_in_progress_articles(db, client, session, cfg, pipeline_clock):
    db.enqueue([found(fox_article(session, "stuck"))])
    db.claim_next(db.now(), 3)
    db.conn.execute("UPDATE articles SET last_attempt_at = now() - interval '5 hours'")

    assert pipeline.work(db, client, cfg, window_minutes=0)["done"] == 1


def test_work_passes_scraper_settings(db, client, session, cfg, pipeline_clock):
    url = fox_article(session, "story")
    session.comment_pages[next(iter(session.comment_pages))]["0"]["paging"] = {"last": False, "next": "p2"}
    db.enqueue([found(url)])
    cfg.scraper.include_replies = False
    cfg.scraper.include_reactions = False
    cfg.scraper.max_pages = 1

    pipeline.work(db, client, cfg, window_minutes=0)
    assert not session.urls("/replies/")
    assert not [c for c in session.calls if c[0] == "POST"]
    assert len(session.urls("/topics/tree/all/")) == 1


def test_work_rescrapes_due_articles_for_new_comments(db, client, session, cfg, pipeline_clock):
    embed = "00000000-0000-0000-0000-000000000042"
    url = fox_article(session, "story", n_comments=2, embed=embed)
    db.enqueue([found(url, published=db.now() - timedelta(hours=30))])
    assert pipeline.work(db, client, cfg, window_minutes=0)["done"] == 1

    # The next run schedules it for 72h after publication; nothing is due yet.
    assert pipeline.work(db, client, cfg, window_minutes=0) == {}
    assert db.queue_summary()["rescrapes_scheduled"] == 1

    session.comment_pages[embed] = {
        "0": page([comment(f"story-c{i}", creator="u1") for i in range(3)], users=[user("u1")]),
    }
    db.conn.execute("UPDATE articles SET rescrape_at = now() - interval '1 minute'")
    assert pipeline.work(db, client, cfg, window_minutes=0) == {"rescraped": 1, "new_comments": 1}
    assert statuses(db) == {"story": "done"}
    assert db.conn.execute("SELECT count(*) AS n FROM comments").fetchone()["n"] == 3

    # A re-scrape that fails keeps the article and its comments.
    del session.pages[url]
    db.conn.execute("UPDATE articles SET rescrape_at = now() - interval '1 minute'")
    assert pipeline.work(db, client, cfg, window_minutes=0) == {"rescrape_failed": 1}
    assert statuses(db) == {"story": "done"}
