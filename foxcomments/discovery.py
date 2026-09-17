"""Find Fox News articles whose titles match keywords."""

import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from .client import FoxCommentsClient

log = logging.getLogger(__name__)

FEED_URL = "https://moxie.foxnews.com/google-publisher/{}.xml"
FEEDS = [
    "latest", "politics", "us", "world", "opinion", "media", "sports",
    "entertainment", "lifestyle", "health", "science", "tech", "travel",
]
SEARCH_URL = "https://api.foxnews.com/search/web"
SEARCH_MAX_RESULTS = 100  # Google Custom Search hard limit
# Articles look like www.foxnews.com/<section>/<slug>; skips video, category, press pages, etc.
ARTICLE_PATH_RE = re.compile(r"^/[a-z0-9-]+/[a-z0-9-]+/?$")
NON_ARTICLE_SECTIONS = {"category", "video", "shows", "person", "podcasts"}


@dataclass
class Article:
    url: str
    title: str
    published: datetime | None = None
    source: str | None = None
    matched_keywords: list[str] = field(default_factory=list)


class TitleMatcher:
    """Case-insensitive whole-word match; "Trump" matches "Trump's" but not "Trumpet"."""

    def __init__(self, keywords: list[str], require_all: bool = False):
        if not keywords:
            raise ValueError("at least one keyword is required")
        self.keywords = keywords
        self.require_all = require_all
        self._patterns = [
            re.compile(r"(?<!\w)" + re.escape(k.strip()) + r"(?!\w)", re.IGNORECASE) for k in keywords
        ]

    def matches(self, title: str) -> list[str]:
        """Keywords found in the title; empty if the title doesn't qualify."""
        found = [k for k, p in zip(self.keywords, self._patterns) if p.search(title)]
        if self.require_all and len(found) != len(self.keywords):
            return []
        return found

    def __call__(self, title: str) -> bool:
        return bool(self.matches(title))


def normalize_url(url: str) -> str:
    return url.strip().split("?", 1)[0].split("#", 1)[0].rstrip("/")


def parse_published(value: str | None) -> datetime | None:
    """Parse RSS pubDate ("Wed, 16 Sep 2026 19:05:04 -0400") or ISO dates ("2026-09-16")."""
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    # Always timezone-aware: "-0000" and bare ISO dates come back naive, and naive/aware can't be compared.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_article_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc != "www.foxnews.com" or not ARTICLE_PATH_RE.match(parsed.path):
        return False
    return parsed.path.split("/")[1] not in NON_ARTICLE_SECTIONS


def from_feeds(client: FoxCommentsClient, feeds: list[str]) -> list[Article]:
    articles = []
    for feed in feeds:
        try:
            root = ET.fromstring(client.get(FEED_URL.format(feed)).content)
        except Exception as exc:
            log.warning("Skipping feed %r: %s", feed, exc)
            continue
        items = root.findall(".//item")
        log.info("Feed %-13s %d articles", feed, len(items))
        for item in items:
            url = normalize_url(item.findtext("link") or "")
            if is_article_url(url):
                articles.append(Article(
                    url=url,
                    title=html.unescape(item.findtext("title") or "").strip(),
                    published=parse_published(item.findtext("pubDate")),
                    source=f"feed:{feed}",
                ))
    return articles


def from_search(client: FoxCommentsClient, query: str, max_results: int = SEARCH_MAX_RESULTS) -> list[Article]:
    """Fox site search, newest first. Capped at 100 results by Google."""
    articles = []
    max_results = min(max_results, SEARCH_MAX_RESULTS)
    for start in range(1, max_results + 1, 10):
        params = {
            "q": query, "siteSearch": "foxnews.com", "siteSearchFilter": "i",
            "sort": "date:r:::", "start": start,
        }
        data = client.get(SEARCH_URL, params=params).json()
        items = data.get("items") or []
        for item in items:
            url = normalize_url(item.get("link", ""))
            if not is_article_url(url):
                continue
            # The search "title" is often a rewritten SEO title; og:title is the real headline.
            meta = ((item.get("pagemap") or {}).get("metatags") or [{}])[0]
            articles.append(Article(
                url=url,
                title=html.unescape(meta.get("og:title") or item.get("title", "")).strip(),
                published=parse_published(meta.get("article:published_time") or meta.get("dc.date")),
                source="search",
            ))
        log.info("Search %r: %d results so far", query, len(articles))
        if not items or not (data.get("queries") or {}).get("nextPage"):
            break
    return articles


def find_articles(
    client: FoxCommentsClient,
    matcher: TitleMatcher,
    feeds: list[str] | None = None,
    search: bool = False,
) -> list[Article]:
    """Collect candidates from feeds and/or search, dedupe by URL, keep title matches."""
    candidates: list[Article] = []
    if feeds:
        candidates += from_feeds(client, feeds)
    if search:
        for keyword in matcher.keywords:
            candidates += from_search(client, keyword)

    seen = set()
    matches = []
    for a in candidates:
        if a.url in seen:
            continue
        seen.add(a.url)
        a.matched_keywords = matcher.matches(a.title)
        if a.matched_keywords:
            matches.append(a)
    log.info("%d unique articles, %d with matching titles", len(seen), len(matches))
    return matches
