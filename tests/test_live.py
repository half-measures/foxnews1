"""Smoke tests against the real Fox News site. Run with: pytest -m live

These catch Fox changing their markup or API, which the offline tests can't.
"""

import pytest

from foxcomments.client import FoxCommentsClient, NoCommentsError
from foxcomments.discovery import from_feeds
from foxcomments.rate_limiter import RateLimiter

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def live_client():
    return FoxCommentsClient(RateLimiter(rate=1, burst=1))


@pytest.fixture(scope="module")
def feed_articles(live_client):
    articles = from_feeds(live_client, ["politics"])
    assert len(articles) >= 5, "politics feed returned too few articles"
    return articles


def test_feed_articles_have_titles_and_dates(feed_articles):
    assert all(a.title and a.url.startswith("https://www.foxnews.com/") for a in feed_articles)
    assert sum(a.published is not None for a in feed_articles) >= len(feed_articles) // 2


def test_article_page_and_comments_api_still_work(live_client, feed_articles):
    for a in feed_articles[:5]:
        try:
            info = live_client.resolve_article(a.url)
        except NoCommentsError:
            continue
        first_page = next(live_client.comment_pages(info["id"]))
        assert {"list2", "objects", "paging"} <= first_page.keys()
        ids = [i["id"] for i in first_page["list2"]]
        if ids:
            metrics = live_client.reaction_metrics(ids[:5])
            assert all("total" in m for m in metrics.values())
        return
    pytest.fail("none of the first 5 politics articles had a comment section")
