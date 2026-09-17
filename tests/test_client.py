import pytest
import requests

from fakes import article_html, comment, make_response, page
from foxcomments.client import API_BASE, FoxCommentsClient, NoCommentsError
from foxcomments.rate_limiter import RateLimiter

EMBED = "e8f5d4ab-a65e-5a98-aae7-b377f610d18e"
URL = "https://www.foxnews.com/politics/some-story"


# --- resolve_article -------------------------------------------------------

def test_resolve_article_reads_embed_id_and_unescapes_title(client, session):
    session.pages[URL] = article_html(EMBED, title="Leno says he gets &#x27;beat up&#x27; &amp; more")
    assert client.resolve_article(URL) == {
        "id": EMBED, "url": URL, "title": "Leno says he gets 'beat up' & more",
    }


def test_resolve_article_accepts_bare_uuid_without_fetching(client, session):
    assert client.resolve_article(EMBED.upper())["id"] == EMBED
    assert session.calls == []


def test_resolve_article_without_comment_section_raises(client, session):
    session.pages[URL] = article_html(None)
    with pytest.raises(NoCommentsError):
        client.resolve_article(URL)


# --- pagination ------------------------------------------------------------

def test_comment_pages_follow_cursor_and_url_encode_it(client, session):
    cursor = "1789601180190|Bdpz4LImEfGuX9D4U6pDLw--"
    session.comment_pages[EMBED] = {
        "0": page([comment("a")], next_cursor=cursor, last=False),
        cursor: page([comment("b")], last=True),
    }
    pages = list(client.comment_pages(EMBED))
    assert [p["list2"][0]["id"] for p in pages] == ["a", "b"]
    assert session.urls()[1].endswith("/topics/tree/all/1789601180190%7CBdpz4LImEfGuX9D4U6pDLw--")


def test_comment_pages_respects_max_pages(client, session):
    session.comment_pages[EMBED] = {
        "0": page([comment("a")], next_cursor="c1", last=False),
        "c1": page([comment("b")], next_cursor="c2", last=False),
    }
    assert len(list(client.comment_pages(EMBED, max_pages=1))) == 1


def test_comment_pages_stops_on_empty_page_even_if_not_last(client, session):
    session.comment_pages[EMBED] = {"0": page([], next_cursor="c1", last=False)}
    assert len(list(client.comment_pages(EMBED))) == 1


def test_reply_pages_paginate(client, session):
    session.reply_pages["top"] = {
        "0": page([comment("r1", parent="top")], next_cursor="20", last=False),
        "20": page([comment("r2", parent="top")], last=True),
    }
    ids = [p["list2"][0]["id"] for p in client.reply_pages("top")]
    assert ids == ["r1", "r2"]


def test_reaction_metrics_batches_requests_and_merges(client, session):
    ids = [f"c{i}" for i in range(5)]
    session.reactions = {i: {"total": n, "reactions": {"Agree": n}} for n, i in enumerate(ids)}
    result = client.reaction_metrics(ids, chunk_size=2)
    posts = [c for c in session.calls if c[0] == "POST"]
    assert [c[2]["json"]["value"] for c in posts] == [["c0", "c1"], ["c2", "c3"], ["c4"]]
    assert result["c3"] == {"total": 3, "reactions": {"Agree": 3}}


# --- retries & backoff -----------------------------------------------------

def test_retries_server_errors_then_succeeds(client, session, clock):
    target = f"{API_BASE}/v2/topics/"
    session.failures[target] = [make_response(503), make_response(502)]
    assert list(client.reply_pages("x"))  # third try hits the normal route
    assert len(session.urls(target)) == 3


def test_honours_retry_after_on_429(clock, session):
    c = FoxCommentsClient(RateLimiter(rate=1, burst=1))
    c.session = session
    session.pages[URL] = article_html(EMBED)
    session.failures[URL] = [make_response(429, headers={"Retry-After": "7"})]
    start = clock.now
    c.resolve_article(URL)
    assert clock.now - start == pytest.approx(7)


def test_gives_up_after_max_retries(clock, session):
    c = FoxCommentsClient(RateLimiter(rate=1000, burst=1000), max_retries=2)
    c.session = session
    session.failures[URL] = [make_response(500) for _ in range(10)]
    with pytest.raises(requests.HTTPError):
        c.get(URL)
    assert len(session.urls(URL)) == 3


def test_does_not_retry_client_errors(client, session):
    with pytest.raises(requests.HTTPError):
        client.get("https://www.foxnews.com/politics/missing")
    assert len(session.calls) == 1


def test_retries_connection_errors_with_backoff(client, session, clock):
    session.pages[URL] = "<html></html>"
    session.failures[URL] = [requests.ConnectionError("boom"), requests.Timeout("slow")]
    assert client.get(URL).status_code == 200
    assert [s for s in clock.sleeps if s >= 1] == [1, 2]


def test_every_request_goes_through_the_rate_limiter(clock, session):
    c = FoxCommentsClient(RateLimiter(rate=2, burst=1))
    c.session = session
    session.pages[URL] = "<html></html>"
    for _ in range(5):
        c.get(URL)
    assert clock.now - 1000 == pytest.approx(2.0)  # 4 gaps of 0.5s
