"""CLI: python -m foxcomments <article-url> [-o out.json]"""

import argparse
import json
import logging
import re
from pathlib import Path

from .client import FoxCommentsClient
from .rate_limiter import RateLimiter
from .scraper import scrape_article


def main() -> None:
    p = argparse.ArgumentParser(description="Scrape Fox News article comments to JSON.")
    p.add_argument("article", help="Article URL or comment-embed UUID")
    p.add_argument("-o", "--output", help="Output JSON path (default: output/<slug>.json)")
    p.add_argument("--rate", type=float, default=1.0, help="Max requests per second (default: 1.0)")
    p.add_argument("--burst", type=int, default=1, help="Max burst size (default: 1)")
    p.add_argument("--max-pages", type=int, help="Stop after N pages of top-level comments (20 per page)")
    p.add_argument("--no-replies", action="store_true", help="Skip fetching replies")
    p.add_argument("--no-reactions", action="store_true", help="Skip fetching reaction counts")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    client = FoxCommentsClient(RateLimiter(rate=args.rate, burst=args.burst))
    result = scrape_article(
        client,
        args.article,
        include_replies=not args.no_replies,
        include_reactions=not args.no_reactions,
        max_pages=args.max_pages,
    )

    if args.output:
        out = Path(args.output)
    else:
        slug = args.article.rstrip("/").rsplit("/", 1)[-1]
        out = Path("output") / f"{re.sub(r'[^A-Za-z0-9_-]', '_', slug)}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    c = result["counts"]
    logging.info("Wrote %d comments + %d replies to %s", c["top_level_comments"], c["replies"], out)


if __name__ == "__main__":
    main()
