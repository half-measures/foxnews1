"""Settings loaded from config.toml (all keys optional), with DATABASE_URL env override."""

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .discovery import FEEDS

DEFAULT_DATABASE_URL = "postgresql://foxcomments:foxcomments@127.0.0.1:5432/foxcomments"


@dataclass
class DiscoveryConfig:
    keywords: list[str] = field(default_factory=lambda: ["Trump"])
    require_all: bool = False
    feeds: list[str] = field(default_factory=lambda: list(FEEDS))
    use_search: bool = False
    max_new_articles: int = 30        # per discover run; 0 = no cap
    min_article_age_hours: float = 24  # wait this long after publication before scraping


@dataclass
class WorkerConfig:
    window_minutes: float = 90    # spread the queue across this long
    min_gap_seconds: float = 30   # never start articles closer together than this
    max_attempts: int = 3         # failed articles are retried on later runs up to this many tries


@dataclass
class ScheduleConfig:
    daily_at: str = "07:00"  # local time the service runs discover + work


@dataclass
class ScraperConfig:
    rate: float = 1.0
    burst: int = 1
    include_replies: bool = True
    include_reactions: bool = True
    store_raw: bool = True  # keep each comment's full API object in comments.raw
    max_pages: int = 0      # 0 = all pages


@dataclass
class Config:
    database_url: str = DEFAULT_DATABASE_URL
    log_dir: str = "logs"
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    scraper: ScraperConfig = field(default_factory=ScraperConfig)


def _fill(cls, data: dict, section: str):
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown key(s) in [{section}]: {', '.join(sorted(unknown))}")
    return cls(**data)


def load_config(path: str | Path | None) -> Config:
    raw = {}
    if path and Path(path).exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)

    db = raw.get("database", {})
    cfg = Config(
        database_url=db.get("url", DEFAULT_DATABASE_URL),
        log_dir=raw.get("logging", {}).get("dir", "logs"),
        discovery=_fill(DiscoveryConfig, raw.get("discovery", {}), "discovery"),
        schedule=_fill(ScheduleConfig, raw.get("schedule", {}), "schedule"),
        worker=_fill(WorkerConfig, raw.get("worker", {}), "worker"),
        scraper=_fill(ScraperConfig, raw.get("scraper", {}), "scraper"),
    )
    cfg.database_url = os.environ.get("DATABASE_URL", cfg.database_url)

    bad_feeds = set(cfg.discovery.feeds) - set(FEEDS)
    if bad_feeds:
        raise ValueError(f"Unknown feed(s): {', '.join(sorted(bad_feeds))}. Choices: {' '.join(FEEDS)}")
    if not cfg.discovery.keywords:
        raise ValueError("[discovery] keywords must not be empty")

    from .service import parse_daily_at  # validate the schedule up front, not at 07:00
    parse_daily_at(cfg.schedule.daily_at)
    return cfg
