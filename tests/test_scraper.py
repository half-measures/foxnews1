import pytest

from fakes import article_html, comment, page, user
from foxcomments.client import NoCommentsError
from foxcomments.scraper import scrape_article

EMBED = "93796465-e099-586d-a059-779807a18d27"
URL = "https://www.foxnews.com/politics/story"


@pytest.fixture
def article(session):
    """Two pages of top-level comments; c1 has two replies across two reply pages."""
    session.pages[URL] = article_html(EMBED, title="Big story")
    session.comment_pages[EMBED] = {
        "0": page([comment("c1", "first", creator="u1"), comment("c2", "second", creator="u2")],
                  users=[user("u1", "alice"), user("u2", "bob")], next_cursor="cur|2", last=False),
        "cur|2": page([comment("c3", "third", creator="u1")], users=[user("u1", "alice")]),
    }
    session.reply_pages["c1"] = {
        "0": page([comment("r1", "reply one", creator="u2", parent="c1")], users=[user("u2", "bob")],
                  next_cursor="20", last=False),
        "20": page([comment("r2", "reply two", creator="u3", parent="c1")], users=[user("u3", "carol")]),
    }
    session.reactions = {
        "c1": {"total": 5, "reactions": {"Agree": 4, "Disagree": 1}},
        "r2": {"total": 1, "reactions": {"Disagree": 1}},
    }
    return session


def test_full_scrape_builds_comment_tree(client, article):
    result = scrape_article(client, URL)

    assert result["article"] == {"id": EMBED, "url": URL, "title": "Big story"}
    assert result["counts"] == {"top_level_comments": 3, "replies": 2}
    assert [c["id"] for c in result["comments"]] == ["c1", "c2", "c3"]

    c1 = result["comments"][0]
    assert c1["body"] == "first"
    assert c1["author"] == {"id": "u1", "username": "alice", "display_name": "alice"}
    assert c1["parent_comment_id"] is None
    assert (c1["reaction_total"], c1["reactions"]) == (5, {"Agree": 4, "Disagree": 1})

    assert [r["id"] for r in c1["replies"]] == ["r1", "r2"]
    assert c1["replies"][1]["author"]["username"] == "carol"
    assert c1["replies"][1]["parent_comment_id"] == "c1"
    assert c1["replies"][1]["reactions"] == {"Disagree": 1}


def test_comments_without_reactions_get_zero(client, article):
    c2 = scrape_article(client, URL)["comments"][1]
    assert (c2["reaction_total"], c2["reactions"]) == (0, {})


def test_reactions_requested_once_for_comments_and_replies(client, article):
    scrape_article(client, URL)
    posts = [c for c in article.calls if c[0] == "POST"]
    assert len(posts) == 1
    assert sorted(posts[0][2]["json"]["value"]) == ["c1", "c2", "c3", "r1", "r2"]


def test_skip_replies_and_reactions(client, article):
    result = scrape_article(client, URL, include_replies=False, include_reactions=False)
    assert result["counts"] == {"top_level_comments": 3, "replies": 0}
    assert not article.urls("/replies/")
    assert not [c for c in article.calls if c[0] == "POST"]
    assert "reactions" not in result["comments"][0]


def test_max_pages_limits_top_level_comments(client, article):
    result = scrape_article(client, URL, max_pages=1, include_replies=False)
    assert [c["id"] for c in result["comments"]] == ["c1", "c2"]


def test_unknown_author_is_kept_with_null_names(client, session):
    session.pages[URL] = article_html(EMBED)
    session.comment_pages[EMBED] = {"0": page([comment("c1", creator="ghost")])}  # no User object
    c1 = scrape_article(client, URL, include_replies=False, include_reactions=False)["comments"][0]
    assert c1["author"] == {"id": "ghost", "username": None, "display_name": None}


def test_listed_comment_missing_from_objects_is_skipped(client, session):
    session.pages[URL] = article_html(EMBED)
    session.comment_pages[EMBED] = {"0": page([comment("c1")], list_ids=["c1", "vanished"])}
    result = scrape_article(client, URL, include_replies=False, include_reactions=False)
    assert [c["id"] for c in result["comments"]] == ["c1"]


def test_article_with_no_comments(client, session):
    session.pages[URL] = article_html(EMBED)
    session.comment_pages[EMBED] = {"0": page([])}
    result = scrape_article(client, URL)
    assert result["counts"] == {"top_level_comments": 0, "replies": 0}
    assert not [c for c in session.calls if c[0] == "POST"]


def test_comments_disabled_raises(client, session):
    session.pages[URL] = article_html(None)
    with pytest.raises(NoCommentsError):
        scrape_article(client, URL)
