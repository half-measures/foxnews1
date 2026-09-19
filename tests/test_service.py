from datetime import datetime, timedelta

import pytest

from fakes import article_html, comment, page, user
from foxcomments import pipeline, service
from foxcomments.config import Config
from foxcomments.discovery import Article


def at(hour, minute=0, day=16):
    return datetime(2026, 9, day, hour, minute)


# --- schedule arithmetic ---------------------------------------------------

@pytest.mark.parametrize("value,expected", [("07:00", (7, 0)), ("7:5", (7, 5)), (" 23:59 ", (23, 59))])
def test_parse_daily_at(value, expected):
    assert service.parse_daily_at(value) == expected


@pytest.mark.parametrize("value", ["7am", "25:00", "07:60", "07", "", "07:00:00"])
def test_parse_daily_at_rejects_nonsense(value):
    with pytest.raises(ValueError, match="daily_at"):
        service.parse_daily_at(value)


def test_occurrences_around_the_scheduled_time():
    assert service.previous_occurrence(at(9), 7, 0) == at(7)
    assert service.next_occurrence(at(9), 7, 0) == at(7, day=17)
    # Before today's slot, "previous" is yesterday's.
    assert service.previous_occurrence(at(6), 7, 0) == at(7, day=15)
    assert service.next_occurrence(at(6), 7, 0) == at(7)
    # Exactly on the minute counts as having happened.
    assert service.previous_occurrence(at(7), 7, 0) == at(7)


@pytest.mark.parametrize("now,last_run,due", [
    (at(9), None, True),                  # never run
    (at(9), at(7, 30), False),            # already ran after today's slot
    (at(9), at(7, 0, day=15), True),      # last ran before today's slot
    (at(6), at(8, 0, day=15), False),     # ran after yesterday's slot, today's hasn't arrived
    (at(6), at(6, 0, day=15), True),      # missed yesterday's slot (machine was off)
])
def test_is_due(now, last_run, due):
    assert service.is_due(now, 7, 0, last_run) is due


# --- the service loop ------------------------------------------------------

@pytest.fixture
def service_clock(clock, monkeypatch):
    monkeypatch.setattr(service, "time", clock)
    monkeypatch.setattr(pipeline, "time", clock)
    monkeypatch.setattr(pipeline.random, "uniform", lambda a, b: 1.0)
    return clock


def test_service_runs_when_due_then_sleeps_until_tomorrow(db, client, session, cfg_for_service, service_clock):
    url = "https://www.foxnews.com/politics/story"
    embed = "11111111-1111-1111-1111-111111111111"
    session.pages[url] = article_html(embed, title="Trump story")
    session.comment_pages[embed] = {"0": page([comment("c1")], users=[user("u1")])}
    db.enqueue([Article(url=url, title="Trump story", source="feed:politics", matched_keywords=["Trump"])])
    session.feeds["politics"] = "<rss><channel></channel></rss>"

    cycles = service.run_service(db, lambda: client, cfg_for_service, max_cycles=2)

    assert cycles == 2
    assert db.conn.execute("SELECT status FROM articles").fetchone()["status"] == "done"
    assert db.last_run_at("daily") is not None
    # Second cycle found nothing due and slept.
    assert service_clock.sleeps and max(service_clock.sleeps) >= 60


def test_service_does_not_rerun_the_same_slot(db, client, session, cfg_for_service, service_clock):
    session.feeds["politics"] = "<rss><channel></channel></rss>"
    service.run_service(db, lambda: client, cfg_for_service, max_cycles=3)
    runs = db.conn.execute("SELECT count(*) AS n FROM runs WHERE kind = 'daily'").fetchone()["n"]
    assert runs == 1


def test_service_keeps_going_after_a_failed_run(db, client, cfg_for_service, service_clock, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("discovery exploded")

    monkeypatch.setattr(pipeline, "find_articles", boom)
    service.run_service(db, lambda: client, cfg_for_service, max_cycles=2)

    run = db.recent_runs()[0]
    assert run["error"] == "RuntimeError: discovery exploded"
    assert run["finished_at"] is not None


def test_shutdown_releases_the_article_like_ctrl_c(db, client, cfg_for_service, service_clock, monkeypatch):
    db.enqueue([Article(url="https://www.foxnews.com/politics/x", title="Trump x",
                        source="feed:politics", matched_keywords=["Trump"])])

    def stop(*a, **k):
        raise service.Shutdown("received signal SIGTERM")

    monkeypatch.setattr(pipeline, "scrape_article", stop)
    with pytest.raises(service.Shutdown):
        pipeline.work(db, client, cfg_for_service)

    row = db.conn.execute("SELECT status, attempts FROM articles").fetchone()
    assert (row["status"], row["attempts"]) == ("pending", 0)


def test_shutdown_is_a_keyboard_interrupt():
    assert issubclass(service.Shutdown, KeyboardInterrupt)


@pytest.fixture
def cfg_for_service():
    cfg = Config()
    cfg.discovery.feeds = ["politics"]
    cfg.discovery.use_search = False
    cfg.worker.window_minutes = 0
    cfg.worker.min_gap_seconds = 1
    # A slot that has already passed today, so the first cycle is due.
    cfg.schedule.daily_at = f"{max(datetime.now().hour - 1, 0):02d}:00"
    return cfg
