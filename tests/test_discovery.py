from datetime import datetime, timezone

import pytest

from fakes import rss
from foxcomments.discovery import (
    Article,
    TitleMatcher,
    find_articles,
    from_feeds,
    from_search,
    is_article_url,
    normalize_url,
    parse_published,
)


# --- TitleMatcher ------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("Trump signs bill", True),
    ("TRUMP signs bill", True),
    ("Critics slam Trump's plan", True),
    ("Trump-backed candidate wins", True),
    ("'Trump' says aide", True),
    ("Trumpet player goes viral", False),
    ("Anti-trumpism is rising", False),
    ("Biden speaks", False),
])
def test_matcher_whole_word_case_insensitive(title, expected):
    assert TitleMatcher(["Trump"])(title) is expected


def test_matcher_any_vs_all():
    any_m = TitleMatcher(["Trump", "Iran"])
    all_m = TitleMatcher(["Trump", "Iran"], require_all=True)
    assert any_m.matches("Trump on tariffs") == ["Trump"]
    assert all_m.matches("Trump on tariffs") == []
    assert all_m.matches("Iran responds to Trump") == ["Trump", "Iran"]


def test_matcher_phrases_and_regex_characters_are_literal():
    assert TitleMatcher(["White House"])("Inside the White House today")
    assert not TitleMatcher(["White House"])("White housing costs")
    assert TitleMatcher(["C++"])("Why C++ still matters")
    assert not TitleMatcher(["a.b"])("axb")


def test_matcher_requires_keywords():
    with pytest.raises(ValueError):
        TitleMatcher([])


# --- URL helpers -------------------------------------------------------------

def test_normalize_url_strips_query_fragment_and_trailing_slash():
    assert normalize_url(" https://www.foxnews.com/us/story/?utm=x#comments ") == "https://www.foxnews.com/us/story"


@pytest.mark.parametrize("url,expected", [
    ("https://www.foxnews.com/politics/some-story-slug", True),
    ("https://www.foxnews.com/politics/some-story-slug/", True),
    ("https://www.foxnews.com/video/6361234567", False),
    ("https://www.foxnews.com/category/politics/elections", False),
    ("https://www.foxnews.com/politics", False),
    ("https://press.foxnews.com/2025/04/fox-noticias", False),
    ("https://www.foxbusiness.com/politics/some-story", False),
])
def test_is_article_url(url, expected):
    assert is_article_url(url) is expected


@pytest.mark.parametrize("value,expected", [
    ("Wed, 16 Sep 2026 19:05:04 -0400", datetime(2026, 9, 16, 23, 5, 4, tzinfo=timezone.utc)),
    ("Wed, 16 Sep 2026 19:05:04 -0000", datetime(2026, 9, 16, 19, 5, 4, tzinfo=timezone.utc)),
    ("2026-09-16", datetime(2026, 9, 16, tzinfo=timezone.utc)),
    ("2026-09-16T10:00:00Z", datetime(2026, 9, 16, 10, tzinfo=timezone.utc)),
    ("not a date", None),
    ("", None),
    (None, None),
])
def test_parse_published(value, expected):
    result = parse_published(value)
    assert result == expected
    if result is not None:
        assert result.tzinfo is not None, "must be timezone-aware so dates can be compared/sorted"


# --- sources -----------------------------------------------------------------

def test_from_feeds_parses_items_and_skips_non_articles(client, session):
    session.feeds["politics"] = rss([
        {"link": "https://www.foxnews.com/politics/story-one?intcmp=rss", "title": "Trump &amp; Congress"},
        {"link": "https://www.foxnews.com/video/12345", "title": "Trump video"},
    ])
    articles = from_feeds(client, ["politics"])
    assert articles == [Article(
        url="https://www.foxnews.com/politics/story-one",
        title="Trump & Congress",
        published=datetime(2026, 9, 16, 23, 5, 4, tzinfo=timezone.utc),
        source="feed:politics",
    )]


def test_from_feeds_skips_broken_feeds_and_keeps_going(client, session):
    session.feeds["world"] = "<rss><channel><item><link>oops"  # malformed XML
    session.feeds["us"] = rss([{"link": "https://www.foxnews.com/us/story", "title": "Ok"}])
    articles = from_feeds(client, ["missing", "world", "us"])
    assert [a.url for a in articles] == ["https://www.foxnews.com/us/story"]


def _search_page(items, has_next):
    return {"items": items, "queries": {"nextPage": [{}]} if has_next else {}}


def test_from_search_pages_and_prefers_og_title(client, session):
    session.search_results["Trump"] = [
        _search_page([
            {"link": "https://www.foxnews.com/politics/a", "title": "SEO title...",
             "pagemap": {"metatags": [{"og:title": "Real Trump headline", "dc.date": "2026-09-15"}]}},
            {"link": "https://press.foxnews.com/2025/x", "title": "Press release"},
        ], has_next=True),
        _search_page([{"link": "https://www.foxnews.com/us/b", "title": "Trump b"}], has_next=False),
    ]
    articles = from_search(client, "Trump")
    assert [(a.url, a.title) for a in articles] == [
        ("https://www.foxnews.com/politics/a", "Real Trump headline"),
        ("https://www.foxnews.com/us/b", "Trump b"),
    ]
    assert articles[0].published == datetime(2026, 9, 15, tzinfo=timezone.utc)
    starts = [c[2]["params"]["start"] for c in session.calls]
    assert starts == [1, 11]


def test_from_search_never_requests_past_google_limit(client, session):
    item = {"link": "https://www.foxnews.com/politics/x", "title": "x"}
    session.search_results["q"] = [_search_page([item], has_next=True)] * 20
    from_search(client, "q", max_results=500)
    assert max(c[2]["params"]["start"] for c in session.calls) == 91


def test_find_articles_dedupes_across_sources_and_records_matches(client, session):
    session.feeds["politics"] = rss([
        {"link": "https://www.foxnews.com/politics/trump-iran", "title": "Trump weighs Iran options"},
        {"link": "https://www.foxnews.com/politics/biden", "title": "Biden speaks"},
    ])
    session.feeds["latest"] = rss([
        {"link": "https://www.foxnews.com/politics/trump-iran/", "title": "Trump weighs Iran options"},
    ])
    session.search_results["Iran"] = [_search_page(
        [{"link": "https://www.foxnews.com/world/iran-only", "title": "Iran vote"}], has_next=False)]

    found = find_articles(client, TitleMatcher(["Trump", "Iran"]), feeds=["politics", "latest"], search=True)
    assert [(a.url, a.matched_keywords) for a in found] == [
        ("https://www.foxnews.com/politics/trump-iran", ["Trump", "Iran"]),
        ("https://www.foxnews.com/world/iran-only", ["Iran"]),
    ]
