import pytest

from foxcomments.config import DEFAULT_DATABASE_URL, load_config
from foxcomments.discovery import FEEDS


@pytest.fixture(autouse=True)
def no_env_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_file_gives_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg.database_url == DEFAULT_DATABASE_URL
    assert "127.0.0.1" in cfg.database_url  # localhost hangs with Docker Desktop on Windows
    assert cfg.discovery.keywords == ["Trump"]
    assert cfg.discovery.feeds == FEEDS
    assert cfg.worker.window_minutes == 90
    assert cfg.scraper.rate == 1.0


def test_example_config_is_valid_and_matches_defaults():
    from pathlib import Path

    example = Path(__file__).parent.parent / "config.example.toml"
    assert load_config(example) == load_config(None)


def test_values_are_loaded(tmp_path):
    cfg = load_config(write(tmp_path, """
[database]
url = "postgresql://x@127.0.0.1/y"
[discovery]
keywords = ["Biden", "Harris"]
require_all = true
feeds = ["politics"]
max_new_articles = 0
[worker]
window_minutes = 30
[scraper]
rate = 0.5
max_pages = 2
"""))
    assert cfg.database_url == "postgresql://x@127.0.0.1/y"
    assert cfg.discovery.keywords == ["Biden", "Harris"]
    assert cfg.discovery.require_all is True
    assert cfg.discovery.feeds == ["politics"]
    assert cfg.discovery.max_new_articles == 0
    assert cfg.worker.window_minutes == 30
    assert cfg.worker.max_attempts == 3  # untouched default
    assert (cfg.scraper.rate, cfg.scraper.max_pages) == (0.5, 2)


def test_env_var_overrides_database_url(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://env@127.0.0.1/env")
    cfg = load_config(write(tmp_path, '[database]\nurl = "postgresql://file@127.0.0.1/file"\n'))
    assert cfg.database_url == "postgresql://env@127.0.0.1/env"


@pytest.mark.parametrize("text,message", [
    ("[worker]\nwindow_minutes = 5\ntypo_key = 1\n", "typo_key"),
    ('[discovery]\nfeeds = ["politics", "nonsense"]\n', "nonsense"),
    ("[discovery]\nkeywords = []\n", "keywords"),
])
def test_invalid_config_is_rejected(tmp_path, text, message):
    with pytest.raises(ValueError, match=message):
        load_config(write(tmp_path, text))
