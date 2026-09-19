"""CLI.

JSON output:
    python -m foxcomments scrape <article-url> [-o out.json]
    python -m foxcomments search <keyword> [<keyword> ...] [--feeds ...] [--search]
    python -m foxcomments <article-url>            (shorthand for scrape)

Database pipeline (settings in config.toml):
    python -m foxcomments db-init
    python -m foxcomments discover [--dry-run]
    python -m foxcomments work [--window MINUTES]
    python -m foxcomments daily                    (discover + work)
    python -m foxcomments status
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .client import FoxCommentsClient
from .config import Config, load_config
from .discovery import FEEDS, TitleMatcher, find_articles
from .rate_limiter import RateLimiter
from .scraper import scrape_article

JSON_COMMANDS = {"scrape", "search"}
DB_COMMANDS = {"db-init", "discover", "work", "daily", "status", "service"}
COMMANDS = JSON_COMMANDS | DB_COMMANDS


def _slug_path(out_dir: Path, url: str) -> Path:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return out_dir / f"{re.sub(r'[^A-Za-z0-9_-]', '_', slug)}.json"


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--rate", type=float, default=1.0, help="Max requests per second (default: 1.0)")
    p.add_argument("--burst", type=int, default=1, help="Max burst size (default: 1)")
    p.add_argument("--max-pages", type=int, help="Stop after N pages of top-level comments (20 per page)")
    p.add_argument("--no-replies", action="store_true", help="Skip fetching replies")
    p.add_argument("--no-reactions", action="store_true", help="Skip fetching reaction counts")
    p.add_argument("--raw", action="store_true",
                   help="Include each comment's full API object under \"raw\" (roughly doubles file size)")
    p.add_argument("-v", "--verbose", action="store_true")


def _scrape_kwargs(args) -> dict:
    return {
        "include_replies": not args.no_replies,
        "include_reactions": not args.no_reactions,
        "include_raw": args.raw,
        "max_pages": args.max_pages,
    }


def cmd_scrape(args, client: FoxCommentsClient) -> None:
    result = scrape_article(client, args.article, **_scrape_kwargs(args))
    out = Path(args.output) if args.output else _slug_path(Path("output"), args.article)
    _write_json(out, result)
    c = result["counts"]
    logging.info("Wrote %d comments + %d replies to %s", c["top_level_comments"], c["replies"], out)


def cmd_search(args, client: FoxCommentsClient) -> None:
    matcher = TitleMatcher(args.keywords, require_all=args.all)
    feeds = [] if args.no_feeds else args.feeds
    articles = find_articles(client, matcher, feeds=feeds, search=args.search)
    if args.limit:
        articles = articles[:args.limit]

    for a in articles:
        logging.info("  match: %s", a.title)
    if args.dry_run or not articles:
        if not articles:
            logging.info("No articles matched %s", args.keywords)
        return

    out_dir = Path(args.output_dir)
    index = {"keywords": args.keywords, "require_all": args.all, "articles": []}
    for i, a in enumerate(articles, 1):
        logging.info("[%d/%d] %s", i, len(articles), a.url)
        published = a.published.isoformat() if a.published else None
        entry = {"url": a.url, "title": a.title, "published": published, "source": a.source}
        try:
            result = scrape_article(client, a.url, **_scrape_kwargs(args))
        except Exception as exc:  # comments disabled, network failure, etc. — keep going
            logging.warning("  skipped: %s", exc)
            entry["error"] = str(exc)
            index["articles"].append(entry)
            continue
        result["article"]["title"] = result["article"]["title"] or a.title
        result["article"]["published"] = published
        path = _slug_path(out_dir, a.url)
        _write_json(path, result)
        entry.update(file=path.name, **result["counts"])
        index["articles"].append(entry)
        logging.info("  %d comments + %d replies -> %s",
                     result["counts"]["top_level_comments"], result["counts"]["replies"], path)

    _write_json(out_dir / "index.json", index)
    logging.info("Done. Index written to %s", out_dir / "index.json")


def _client_from_config(cfg: Config) -> FoxCommentsClient:
    return FoxCommentsClient(RateLimiter(rate=cfg.scraper.rate, burst=cfg.scraper.burst))


def cmd_db(args, cfg: Config) -> None:
    from . import pipeline
    from .db import Database

    with Database(cfg.database_url) as db:
        db.init_schema()
        if args.command == "db-init":
            logging.info("Schema is up to date")
            return
        if args.command == "status":
            _print_status(db)
            return

        if args.command == "service":
            from .service import install_shutdown_handlers, run_service

            install_shutdown_handlers()
            try:
                run_service(db, lambda: _client_from_config(cfg), cfg)
            except KeyboardInterrupt as exc:
                logging.info("Stopping: %s", exc or "interrupted")
            return

        client = _client_from_config(cfg)
        dry_run = getattr(args, "dry_run", False)
        run_id = None if dry_run else db.start_run(args.command)
        stats = {}
        try:
            if args.command in ("discover", "daily"):
                stats["discover"] = pipeline.discover(db, client, cfg, dry_run=dry_run)
            if args.command in ("work", "daily"):
                stats["work"] = pipeline.work(db, client, cfg, window_minutes=args.window)
        except BaseException as exc:
            if run_id:
                db.finish_run(run_id, stats, error=f"{type(exc).__name__}: {exc}")
            raise
        if run_id:
            db.finish_run(run_id, stats)


def _print_status(db) -> None:
    summary = db.queue_summary()
    print(f"Articles by status: {summary['articles'] or '{}'}")
    print(f"Comments stored: {summary['comments']}   Authors: {summary['authors']}"
          + (f"   Waiting to mature: {summary['waiting']}" if summary["waiting"] else ""))
    print("\nRecent articles:")
    for a in db.recent_articles():
        counts = f"{a['top_level_count']}+{a['reply_count']}" if a["top_level_count"] is not None else "-"
        if a["status"] == "pending" and a["scrape_after"] > datetime.now(timezone.utc):
            counts = f"@{a['scrape_after'].astimezone():%m-%d %H:%M}"
        print(f"  {a['discovered_at'].astimezone():%Y-%m-%d %H:%M}  {a['status']:<11} {counts:>9}  {a['title'][:80]}")
        if a["last_error"] and a["status"] != "done":
            print(f"{'':30}! {a['last_error'][:100]}")
    print("\nRecent runs:")
    for r in db.recent_runs():
        end = f"{r['finished_at'].astimezone():%H:%M}" if r["finished_at"] else "running/killed"
        print(f"  #{r['id']:<4} {r['kind']:<8} {r['started_at'].astimezone():%Y-%m-%d %H:%M} -> {end}  {r['stats']}"
              + (f"  ERROR: {r['error']}" if r["error"] else ""))


def _setup_logging(verbose: bool, log_file: Path | None = None) -> None:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    handlers = [logging.StreamHandler()]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S" if log_file else "%H:%M:%S",
        handlers=handlers,
    )


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv = ["scrape", *argv]  # keep `python -m foxcomments <url>` working

    parser = argparse.ArgumentParser(prog="foxcomments", description="Scrape Fox News comments to JSON.")
    sub = parser.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("scrape", help="Scrape one article")
    sp.add_argument("article", help="Article URL or comment-embed UUID")
    sp.add_argument("-o", "--output", help="Output JSON path (default: output/<slug>.json)")
    _add_common(sp)

    se = sub.add_parser("search", help="Find articles with keywords in the title and scrape them")
    se.add_argument("keywords", nargs="+", help="Keywords to match in titles (case-insensitive, whole word)")
    se.add_argument("--all", action="store_true", help="Require all keywords (default: any)")
    se.add_argument("--feeds", nargs="+", default=FEEDS, metavar="FEED",
                    help=f"RSS feeds to scan (default: all). Choices: {' '.join(FEEDS)}")
    se.add_argument("--no-feeds", action="store_true", help="Don't scan RSS feeds")
    se.add_argument("--search", action="store_true",
                    help="Also use Fox site search (reaches older articles, up to 100 results per keyword)")
    se.add_argument("--limit", type=int, help="Max number of articles to scrape")
    se.add_argument("--dry-run", action="store_true", help="List matching articles without scraping")
    se.add_argument("--output-dir", default="output/search", help="Directory for results (default: output/search)")
    _add_common(se)

    for name, help_text in [
        ("db-init", "Create/upgrade database tables"),
        ("discover", "Find new matching articles and add them to the queue"),
        ("work", "Scrape queued articles, spread across a time window"),
        ("daily", "discover, then work (what the scheduled task runs)"),
        ("status", "Show queue and database summary"),
        ("service", "Stay running and do the daily run at the scheduled time"),
    ]:
        dp = sub.add_parser(name, help=help_text)
        dp.add_argument("-c", "--config", default="config.toml", help="Config file (default: config.toml)")
        dp.add_argument("-v", "--verbose", action="store_true")
        if name == "discover":
            dp.add_argument("--dry-run", action="store_true", help="Show what would be queued without queueing")
        if name in ("work", "daily"):
            dp.add_argument("--window", type=float, help="Override worker window in minutes (0 = no spacing)")

    args = parser.parse_args(argv)

    if args.command in DB_COMMANDS:
        cfg = load_config(args.config)
        log_file = None
        if args.command in ("discover", "work", "daily", "service") and cfg.log_dir:
            log_file = Path(cfg.log_dir) / f"foxcomments-{datetime.now():%Y-%m-%d}.log"
        _setup_logging(args.verbose, log_file)
        cmd_db(args, cfg)
        return

    if args.command == "search" and args.no_feeds and not args.search:
        parser.error("--no-feeds requires --search (otherwise there is nothing to scan)")

    _setup_logging(args.verbose)
    client = FoxCommentsClient(RateLimiter(rate=args.rate, burst=args.burst))
    {"scrape": cmd_scrape, "search": cmd_search}[args.command](args, client)


if __name__ == "__main__":
    main()
