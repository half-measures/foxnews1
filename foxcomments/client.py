"""Client for Fox News' comment system ("hedgehog", served from api.community.fox.com)."""

import logging
import re
import time
from urllib.parse import quote

import requests

from .rate_limiter import RateLimiter

log = logging.getLogger(__name__)

API_BASE = "https://api.community.fox.com"
TENANT = 0  # foxnews.com
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
EMBED_ID_RE = re.compile(r'<hedgehog-comment-embed[^>]*\bid="([0-9a-f-]{36})"', re.I)
TITLE_RE = re.compile(r'<meta[^>]*\bproperty="og:title"[^>]*\bcontent="([^"]*)"', re.I)


class FoxCommentsClient:
    def __init__(self, limiter: RateLimiter, max_retries: int = 4, timeout: float = 20.0):
        self.limiter = limiter
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Origin": "https://www.foxnews.com",
            "Referer": "https://www.foxnews.com/",
        })

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise
                backoff = 2 ** attempt
                log.warning("%s %s failed (%s); retrying in %ss", method, url, exc, backoff)
                time.sleep(backoff)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries:
                    resp.raise_for_status()
                retry_after = resp.headers.get("Retry-After")
                backoff = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                log.warning("%s %s -> %s; backing off %ss", method, url, resp.status_code, backoff)
                self.limiter.penalize(backoff)
                continue

            resp.raise_for_status()
            return resp
        raise RuntimeError("unreachable")

    def _api_get(self, path: str) -> dict:
        return self._request("GET", f"{API_BASE}/{path}").json()

    # --- article ---------------------------------------------------------

    def resolve_article(self, url_or_id: str) -> dict:
        """Return {'id', 'url', 'title'} for an article URL or a bare comment-embed UUID."""
        if UUID_RE.match(url_or_id):
            return {"id": url_or_id.lower(), "url": None, "title": None}
        html = self._request("GET", url_or_id).text
        m = EMBED_ID_RE.search(html)
        if not m:
            raise ValueError(f"No comment section found on {url_or_id} (comments may be disabled)")
        title = TITLE_RE.search(html)
        return {"id": m.group(1), "url": url_or_id, "title": title.group(1) if title else None}

    # --- comments --------------------------------------------------------

    def comment_pages(self, article_id: str, max_pages: int | None = None):
        """Yield raw API pages of top-level comments (newest first)."""
        cursor = "0"
        pages = 0
        while True:
            data = self._api_get(f"v2/post/{TENANT}/{quote(article_id, safe='')}/topics/tree/all/{quote(cursor, safe='')}")
            yield data
            pages += 1
            paging = data.get("paging") or {}
            if paging.get("last") or not data.get("list2") or (max_pages and pages >= max_pages):
                return
            cursor = paging["next"]

    def reply_pages(self, comment_id: str):
        """Yield raw API pages of replies to a top-level comment."""
        cursor = "0"
        while True:
            data = self._api_get(f"v2/topics/{TENANT}/{quote(comment_id, safe='')}/replies/all/{quote(cursor, safe='')}")
            yield data
            paging = data.get("paging") or {}
            if paging.get("last") or not data.get("list2"):
                return
            cursor = paging["next"]

    def reaction_metrics(self, content_ids: list[str], chunk_size: int = 50) -> dict[str, dict]:
        """Return {content_id: {'total': n, 'reactions': {'Agree': n, ...}}}."""
        out = {}
        for i in range(0, len(content_ids), chunk_size):
            chunk = content_ids[i:i + chunk_size]
            resp = self._request("POST", f"{API_BASE}/v3/{TENANT}/reaction/metrics", json={"value": chunk})
            for obj in resp.json().get("objects", []):
                out[obj["content"]] = {"total": obj.get("total", 0), "reactions": obj.get("reactions", {})}
        return out
