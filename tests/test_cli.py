import json
import sys

import pytest

import foxcomments.__main__ as cli

URL = "https://www.foxnews.com/politics/some-story"


@pytest.fixture
def fake_scrape(monkeypatch):
    calls = []

    def scrape(client, url, **kwargs):
        calls.append((url, kwargs))
        return {"article": {"id": "x", "url": url, "title": "T"}, "counts": {"top_level_comments": 0, "replies": 0},
                "comments": []}

    monkeypatch.setattr(cli, "scrape_article", scrape)
    return calls


def run(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["foxcomments", *args])
    cli.main()


@pytest.mark.parametrize("prefix", [[], ["scrape"]])
def test_scrape_writes_json_with_or_without_subcommand(monkeypatch, tmp_path, fake_scrape, prefix):
    out = tmp_path / "out.json"
    run(monkeypatch, *prefix, URL, "-o", str(out), "--no-replies", "--max-pages", "2")
    assert json.loads(out.read_text(encoding="utf-8"))["article"]["url"] == URL
    assert fake_scrape == [(URL, {"include_replies": False, "include_reactions": True, "max_pages": 2})]


def test_scrape_default_output_path_uses_slug(monkeypatch, tmp_path, fake_scrape):
    monkeypatch.chdir(tmp_path)
    run(monkeypatch, URL + "/")
    assert (tmp_path / "output" / "some-story.json").exists()


def test_search_no_feeds_requires_search(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        run(monkeypatch, "search", "Trump", "--no-feeds")
    assert exc.value.code == 2
    assert "--no-feeds requires --search" in capsys.readouterr().err


def test_unknown_feed_rejected_by_config(monkeypatch, tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[discovery]\nfeeds = ["bogus"]\n', encoding="utf-8")
    with pytest.raises(ValueError, match="bogus"):
        run(monkeypatch, "status", "-c", str(cfg))
