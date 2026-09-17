"""Test doubles: a fake clock and an in-memory fake of the Fox endpoints we call."""

import json
from collections import defaultdict
from urllib.parse import unquote, urlparse

import requests

API = "https://api.community.fox.com"


class FakeClock:
    """Stands in for the `time` module: sleeping just advances the clock."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(seconds, 0)


def make_response(status: int = 200, body=b"", headers: dict | None = None, url: str = "") -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
    resp._content = body.encode() if isinstance(body, str) else body
    resp.headers.update(headers or {})
    resp.url = url
    resp.reason = "Test"
    return resp


# --- API object factories (shapes copied from real responses) -------------

def user(uid: str, username: str | None = None) -> dict:
    username = username or f"name_{uid}"
    return {"id": uid, "typename": "User", "username": username, "name": username}


def comment(cid: str, body: str = "", creator: str = "u1", parent: str | None = None, **extra) -> dict:
    return {
        "id": cid, "typename": "Comment", "body": body or f"body of {cid}",
        "created": "2026-09-16T12:00:00.000Z", "updated": "2026-09-16T12:00:05.000Z",
        "creator": creator, "edited": False, "deleted": False, "pinned": False, "score": 0,
        "parentComment": parent, "threadParent": parent, "images": [], "videos": [], **extra,
    }


def page(comments: list[dict], users: list[dict] = (), next_cursor: str = "0", last: bool = True,
         list_ids: list[str] | None = None) -> dict:
    ids = list_ids if list_ids is not None else [c["id"] for c in comments]
    return {
        "list2": [{"id": i, "typename": "Comment"} for i in ids],
        "objects": [*comments, *users],
        "paging": {"last": last, "prev": "0", "next": next_cursor},
    }


def article_html(embed_id: str | None, title: str = "A headline") -> str:
    """Mirrors the real markup: extra attributes before property/content and before id."""
    meta = f'<meta data-n-head="ssr" data-hid="og:title" property="og:title" content="{title}">'
    embed = (
        f'<hedgehog-comment-embed id="{embed_id}" options="{{&quot;appName&quot;:&quot;foxnews.com&quot;}}">'
        "</hedgehog-comment-embed>"
        if embed_id else ""
    )
    return f"<html><head>{meta}</head><body><div class='hedgehog-container'>{embed}</div></body></html>"


def rss(items: list[dict]) -> str:
    body = "".join(
        f"<item><link>{i['link']}</link><title>{i['title']}</title>"
        f"<pubDate>{i.get('pubDate', 'Wed, 16 Sep 2026 19:05:04 -0400')}</pubDate></item>"
        for i in items
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><rss><channel><title>Feed</title>{body}</channel></rss>'


class FakeSession:
    """Replaces requests.Session on FoxCommentsClient. Configure the dicts, then inspect `calls`."""

    def __init__(self):
        self.headers = {}
        self.calls: list[tuple[str, str, dict]] = []
        self.pages: dict[str, str] = {}                       # article url -> html
        self.comment_pages: dict[str, dict[str, dict]] = {}   # embed id -> cursor -> page
        self.reply_pages: dict[str, dict[str, dict]] = {}     # comment id -> cursor -> page
        self.reactions: dict[str, dict] = {}                  # content id -> metric
        self.feeds: dict[str, str] = {}                       # feed name -> xml
        self.search_results: dict[str, list[dict]] = {}       # query -> list of search pages
        self.failures: dict[str, list] = defaultdict(list)    # url substring -> queued responses/exceptions

    def request(self, method, url, params=None, json=None, timeout=None, **_):
        self.calls.append((method, url, {"params": params, "json": json}))
        for pattern, queue in self.failures.items():
            if pattern in url and queue:
                item = queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
        return self._route(method, url, params or {}, json)

    def urls(self, contains: str = "") -> list[str]:
        return [u for _, u, _ in self.calls if contains in u]

    def _route(self, method, url, params, body):
        path = urlparse(url).path
        parts = [unquote(p) for p in path.split("/")]
        not_found = make_response(404, "not found", url=url)

        if url.startswith(API + "/v2/post/"):          # /v2/post/0/{id}/topics/tree/all/{cursor}
            return self._json(self.comment_pages.get(parts[4], {}).get(parts[-1]), url)
        if url.startswith(API + "/v2/topics/"):        # /v2/topics/0/{cid}/replies/all/{cursor}
            pages = self.reply_pages.get(parts[4], {})
            return self._json(pages.get(parts[-1], {"list2": [], "objects": [], "paging": {"last": True}}), url)
        if url == API + "/v3/0/reaction/metrics" and method == "POST":
            objs = [{"content": i, **self.reactions[i]} for i in body["value"] if i in self.reactions]
            return make_response(200, {"objects": objs}, url=url)
        if "moxie.foxnews.com/google-publisher/" in url:
            feed = path.rsplit("/", 1)[-1].removesuffix(".xml")
            return make_response(200, self.feeds[feed], url=url) if feed in self.feeds else not_found
        if url.startswith("https://api.foxnews.com/search/web"):
            pages = self.search_results.get(params["q"], [])
            idx = (int(params["start"]) - 1) // 10
            return make_response(200, pages[idx] if idx < len(pages) else {"queries": {}}, url=url)
        if url in self.pages:
            return make_response(200, self.pages[url], url=url)
        return not_found

    @staticmethod
    def _json(data, url):
        return make_response(200, data, url=url) if data is not None else make_response(404, "nope", url=url)
