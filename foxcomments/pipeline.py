"""Daily pipeline: discover new matching articles, then scrape the queue spread over a time window."""

import logging
import random
import time
from collections import Counter
from datetime import datetime, timezone

from .client import FoxCommentsClient, NoCommentsError
from .config import Config
from .db import Database
from .discovery import TitleMatcher, find_articles
from .scraper import scrape_article

log = logging.getLogger(__name__)


def discover(db: Database, client: FoxCommentsClient, cfg: Config, dry_run: bool = False) -> dict:
    d = cfg.discovery
    matcher = TitleMatcher(d.keywords, require_all=d.require_all)
    found = find_articles(client, matcher, feeds=d.feeds, search=d.use_search)

    known = db.known_urls([a.url for a in found])
    new = [a for a in found if a.url not in known]
    # Newest first, so the cap keeps the freshest articles.
    new.sort(key=lambda a: a.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    capped = new[:d.max_new_articles] if d.max_new_articles else new

    for a in capped:
        log.info("  new: [%s] %s", ", ".join(a.matched_keywords), a.title)
    if len(new) > len(capped):
        log.info("  (%d more new matches over the cap of %d; they'll be picked up next run if still in the feeds)",
                 len(new) - len(capped), d.max_new_articles)

    queued = 0 if dry_run else db.enqueue(capped)
    stats = {"matched": len(found), "already_known": len(known), "new": len(new), "queued": queued}
    log.info("Discovery: %s%s", stats, " (dry run, nothing queued)" if dry_run else "")
    return stats


def work(db: Database, client: FoxCommentsClient, cfg: Config, window_minutes: float | None = None) -> dict:
    """Scrape every ready article, pacing them evenly so the queue finishes near the end of the window."""
    w, s = cfg.worker, cfg.scraper
    window = (window_minutes if window_minutes is not None else w.window_minutes) * 60
    deadline = time.monotonic() + window
    run_started = db.now()
    stats = Counter()

    reset = db.reset_stale()
    if reset:
        log.info("Re-queued %d stale in-progress article(s)", reset)

    total = db.count_ready(run_started, w.max_attempts)
    log.info("Queue: %d article(s) ready; window %.0f min", total, window / 60)

    while (job := db.claim_next(run_started, w.max_attempts)) is not None:
        log.info("Scraping (attempt %d): %s", job["attempts"], job["title"])
        started = time.monotonic()
        try:
            result = scrape_article(
                client, job["url"],
                include_replies=s.include_replies,
                include_reactions=s.include_reactions,
                max_pages=s.max_pages or None,
            )
        except NoCommentsError as exc:
            db.mark_no_comments(job["id"], str(exc))
            stats["no_comments"] += 1
            log.info("  no comment section")
        except KeyboardInterrupt:
            db.release(job["id"])
            log.warning("Interrupted; article returned to the queue")
            raise
        except Exception as exc:
            db.mark_failed(job["id"], f"{type(exc).__name__}: {exc}")
            stats["failed"] += 1
            log.exception("  failed")
        else:
            db.save_scrape(job["id"], result)
            c = result["counts"]
            stats["done"] += 1
            stats["comments"] += c["top_level_comments"]
            stats["replies"] += c["replies"]
            log.info("  saved %d comments + %d replies in %.0fs",
                     c["top_level_comments"], c["replies"], time.monotonic() - started)

        remaining = db.count_ready(run_started, w.max_attempts)
        if remaining == 0:
            break
        time_left = deadline - time.monotonic()
        gap = max(w.min_gap_seconds, time_left / remaining) * random.uniform(0.75, 1.25)
        log.info("%d left; next article in %.1f min", remaining, gap / 60)
        time.sleep(gap)

    log.info("Work finished: %s", dict(stats))
    return dict(stats)
