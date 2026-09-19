"""Long-running service: runs the daily pipeline at a fixed local time.

Pure Python, no cron or Task Scheduler. Run it under systemd (see deploy/) or
in a container; it survives restarts by recording each run in the database, so
a reboot near the scheduled time doesn't silently skip a day.
"""

import logging
import signal
import time
from datetime import datetime, timedelta

from . import pipeline
from .config import Config
from .db import Database

log = logging.getLogger(__name__)

MAX_SLEEP_SECONDS = 3600  # wake up hourly even when the next run is far off


class Shutdown(KeyboardInterrupt):
    """Asked to stop (SIGTERM/SIGINT).

    Subclasses KeyboardInterrupt so the scrape loop's existing handler releases
    the in-flight article back to the queue instead of leaving it in_progress.
    """


def install_shutdown_handlers() -> None:
    def handler(signum, _frame):
        raise Shutdown(f"received signal {signal.Signals(signum).name}")

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handler)


def parse_daily_at(value: str) -> tuple[int, int]:
    """"07:00" -> (7, 0)."""
    try:
        hour, minute = (int(part) for part in value.strip().split(":"))
    except ValueError:
        raise ValueError(f"[schedule] daily_at must look like \"07:00\", got {value!r}") from None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"[schedule] daily_at is not a valid time of day: {value!r}")
    return hour, minute


def previous_occurrence(now: datetime, hour: int, minute: int) -> datetime:
    """The most recent time the clock passed hour:minute (today or yesterday)."""
    today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return today if today <= now else today - timedelta(days=1)


def next_occurrence(now: datetime, hour: int, minute: int) -> datetime:
    return previous_occurrence(now, hour, minute) + timedelta(days=1)


def is_due(now: datetime, hour: int, minute: int, last_run: datetime | None) -> bool:
    """Due when no run has happened since the last scheduled time (so a missed slot catches up)."""
    if last_run is None:
        return True
    return last_run < previous_occurrence(now, hour, minute)


def run_daily(db: Database, client, cfg: Config) -> dict:
    """One discover + work cycle, recorded in the runs table. Errors are logged, not raised."""
    run_id = db.start_run("daily")
    stats: dict = {}
    try:
        stats["discover"] = pipeline.discover(db, client, cfg)
        stats["work"] = pipeline.work(db, client, cfg)
    except Shutdown as exc:
        db.finish_run(run_id, stats, error=str(exc) or "shutdown")
        raise
    except Exception as exc:
        log.exception("Daily run failed")
        db.finish_run(run_id, stats, error=f"{type(exc).__name__}: {exc}")
    else:
        db.finish_run(run_id, stats)
    return stats


def run_service(db: Database, client_factory, cfg: Config, max_cycles: int | None = None) -> int:
    """Loop until stopped. `max_cycles` bounds the loop for tests."""
    hour, minute = parse_daily_at(cfg.schedule.daily_at)
    log.info("Service started: daily run at %02d:%02d local time", hour, minute)
    cycles = 0

    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        now = datetime.now().astimezone()
        last_run = db.last_run_at("daily")

        if is_due(now, hour, minute, last_run):
            run_daily(db, client_factory(), cfg)
            db.disconnect()  # nothing to say until the next run
            continue

        wait = min((next_occurrence(now, hour, minute) - now).total_seconds(), MAX_SLEEP_SECONDS)
        log.info("Next run at %s; sleeping %.0f min",
                 next_occurrence(now, hour, minute).strftime("%Y-%m-%d %H:%M"), wait / 60)
        db.disconnect()
        time.sleep(max(wait, 1))

    return cycles
