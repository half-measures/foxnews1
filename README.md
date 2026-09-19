# foxcomments

Scrapes Fox News article comments. It can write JSON files, or run as a daily job that stores comments in a local PostgreSQL database.

Fox News comments run on Fox's own "hedgehog" platform (`api.community.fox.com`). No login is needed to read them.

## Setup

```
pip install -r requirements.txt
```

## Daily database pipeline

### First-time setup

```
docker compose up -d --wait             # Postgres 17 on 127.0.0.1:5432
cp config.example.toml config.toml      # then edit keywords etc.
python -m foxcomments db-init           # create tables (also runs automatically)
python -m foxcomments discover --dry-run
```

Use `127.0.0.1` in the database URL rather than `localhost`: with Docker Desktop on Windows, `localhost` resolves to IPv6 first and the connection hangs.

Deploying to a headless Linux host is in **[deploy/README.md](deploy/README.md)**.

### How a daily run works

`python -m foxcomments daily` does two steps:

1. **Discover.** It scans the configured feeds (and site search, if `use_search = true`) for titles that match your keywords. It skips articles already in the database. Up to `max_new_articles` of the newest matches are added to the queue as `pending`. Extra matches beyond the cap aren't queued. They're found again next run if they're still in the feeds.
2. **Work.** It scrapes every ready article, spacing them so the queue finishes close to the end of `window_minutes`. For example, 10 articles in 90 minutes means about one every 9 minutes, with ±25% randomness. The wait is recalculated after each article, so slow scrapes don't push the run past the window.

**Articles mature before they're scraped.** Comments keep arriving for a day or two after publication, and each article is scraped only once, so scraping a fresh article would capture almost nothing. Every article gets a `scrape_after` time of `min_article_age_hours` (24 by default) past its publication date, and the work step ignores it until then. Discovery still records it immediately, because feeds drop articles within hours and a missed article never comes back. So an article found this morning is typically scraped by tomorrow's run. Articles with no publication date (site search sometimes omits it) wait that long from when they were found. Set `min_article_age_hours = 0` to scrape as soon as an article is found.

Each article is scraped once, at least `min_article_age_hours` after it was published. Its status moves through `pending` → `in_progress` → one of:
- `done`
- `no_comments`: the article has comments disabled
- `failed`: an error. It's retried on later runs until it has had `max_attempts` tries.

If the process is killed partway through an article, that article goes back to `pending` on the next run. On Ctrl+C this happens right away.

Each run gets a row in the `runs` table. Logs go to `logs/foxcomments-YYYY-MM-DD.log`.

You can run the steps separately with `discover` and `work --window 30`. `work --window 0` scrapes without spacing. `status` shows queue counts, recent articles and recent runs.

### Running it unattended

```
python -m foxcomments service
```

This stays running and does the daily run at `daily_at` (07:00 by default, local time). It's plain Python — no cron, no Task Scheduler. It wakes hourly, so a machine that was off at 07:00 catches up when it comes back instead of skipping the day, and because each run is recorded in the `runs` table it runs at most once per scheduled slot.

On SIGTERM or SIGINT it puts the article it was working on back in the queue, then exits, so a restart never strands one.

**[deploy/README.md](deploy/README.md) has the headless Linux setup**: a systemd unit, pointing at a remote database, log rotation, and how to reach the database for ETL.

`python -m foxcomments daily` is still a one-shot run that exits when finished, if you prefer cron or a systemd timer. Concurrent runs are safe: articles are claimed with `FOR UPDATE SKIP LOCKED`, so two processes never scrape the same one.

### Database connections

The connection is opened when there's something to read or write and dropped during the long gaps between articles, rather than held open for the whole window. That matters for a database on another host, where an idle session gets cut by a firewall, a NAT table or `idle_session_timeout`. Connections also use TCP keepalives, retry 3 times with backoff when the server isn't reachable, and reconnect once automatically if the connection died between uses.

Set `DATABASE_URL` to point at a database anywhere; it overrides `config.toml` and keeps the password out of the repo.

### Tables

| Table | Contents |
|---|---|
| `articles` | One row per discovered article, including queue status, matched keywords, publish date, `scrape_after` (when it becomes eligible) and comment counts |
| `comments` | Top-level comments and replies (`parent_comment_id` is set on replies), with body, timestamps, `reactions` JSON, `agree_count` / `disagree_count` columns, and `raw` holding the untouched API object |
| `authors` | Commenter id, username and display name, plus first and last seen times |
| `runs` | Stats and errors for each run |

Example queries:

```sql
-- Most-agreed comments on Trump articles this week
SELECT a.title, u.username, c.agree_count, left(c.body, 120)
FROM comments c JOIN articles a ON a.id = c.article_id LEFT JOIN authors u ON u.id = c.author_id
WHERE 'Trump' = ANY(a.matched_keywords) AND a.published_at > now() - interval '7 days'
ORDER BY c.agree_count DESC LIMIT 20;

-- Most active commenters
SELECT u.username, count(*) FROM comments c JOIN authors u ON u.id = c.author_id
GROUP BY u.username ORDER BY count(*) DESC LIMIT 20;
```

Open a SQL shell with `docker exec -it foxcomments-db psql -U foxcomments`.

## JSON output

### Usage

#### Scrape one article

```
python -m foxcomments scrape "https://www.foxnews.com/politics/some-article-slug"
python -m foxcomments "https://www.foxnews.com/politics/some-article-slug"   # same thing
```

Output goes to `output/<slug>.json` by default (change it with `-o`).

#### Search by title keywords

```
# Latest articles with "Trump" in the headline, across all section feeds
python -m foxcomments search Trump

# Titles containing BOTH words; add site search to reach older articles
python -m foxcomments search Trump Iran --all --search

# Just list the matches, don't scrape anything
python -m foxcomments search Trump --dry-run

# Only scan certain feeds, and cap the number of articles scraped
python -m foxcomments search Biden Harris --feeds politics media --limit 10
```

Keyword matching ignores case and matches whole words only. "Trump" matches "Trump's" but not "Trumpet". Multi-word phrases work too if you quote them: `"White House"`.

Where the articles come from:
- **RSS feeds** (on by default): the ~25 latest articles in each section (`latest politics us world opinion media sports entertainment lifestyle health science tech travel`).
- **Site search** (`--search`): Fox's own search, newest first. Google caps it at 100 results per keyword. The titles it returns are checked again with the same keyword filter.

Each article is saved to `output/search/<slug>.json`. A summary is written to `output/search/index.json` with each article's title, URL, source and comment counts. If an article has comments disabled, it's recorded in the index with an `error` and the run continues.

| Search flag | Default | Description |
|---|---|---|
| `--all` | any | Require every keyword to appear in the title |
| `--feeds` | all | Which RSS feeds to scan |
| `--no-feeds` | | Skip RSS (use with `--search`) |
| `--search` | off | Also query Fox site search |
| `--limit` | none | Max articles to scrape |
| `--dry-run` | | List matches only |
| `--output-dir` | `output/search` | Where results go |

#### Options for both commands

| Flag | Default | Description |
|---|---|---|
| `--rate` | `1.0` | Max requests per second |
| `--burst` | `1` | Token-bucket burst size |
| `--max-pages` | all | Limit top-level comment pages (20 comments each) |
| `--no-replies` | | Skip replies (one extra request per top-level comment) |
| `--no-reactions` | | Skip Agree/Disagree counts |
| `--raw` | off | Include each comment's full API object under `raw` (roughly doubles file size) |
| `-v` | | Debug logging |

You can also pass the comment-embed UUID directly instead of a URL.

## Tests

```
pip install -r requirements-dev.txt
pytest              # offline tests, about 2s
pytest -m live      # also checks the real Fox site still works the way the scraper expects
```

- **Offline tests** never touch the network. A fake Fox server (`tests/fakes.py`) plays back API responses shaped like the real ones. A fake clock makes rate limiting, backoff and queue spacing run instantly.
- **Database and pipeline tests** use a separate `foxcomments_test` database. It's created automatically on the Docker Postgres and wiped before each test. If Postgres isn't running, these tests are skipped. To use a different server, set `TEST_DATABASE_URL`; the database name must end in `_test`.
- **Live tests** are off by default. Run them now and then to catch Fox changing their markup or API.

## How it works

1. Loads the article HTML and reads the id from `<hedgehog-comment-embed id="...">`.
2. `GET /v2/post/0/{articleId}/topics/tree/all/{cursor}`: top-level comments, newest first, 20 per page.
3. `GET /v2/topics/0/{commentId}/replies/all/{offset}`: replies to each comment.
4. `POST /v3/0/reaction/metrics` `{"value": [ids]}`: reaction counts, in batches of 50.

Every request goes through one shared rate limiter, including feed and search requests. The limiter is a token-bucket `RateLimiter`. A 429 or 5xx response triggers a backoff, which uses `Retry-After` if the server sends it. Otherwise the wait grows exponentially, and the bucket is drained so later requests slow down too.

## Output shape

```json
{
  "article": {"id": "...", "url": "...", "title": "..."},
  "scraped_at": "2026-09-16T23:40:00+00:00",
  "counts": {"top_level_comments": 354, "replies": 613},
  "comments": [
    {
      "id": "...", "body": "...", "created": "...", "updated": "...",
      "edited": false, "deleted": false, "pinned": false, "score": 0,
      "parent_comment_id": null,
      "author": {"id": "...", "username": "...", "display_name": "..."},
      "images": [], "videos": [],
      "reaction_total": 8, "reactions": {"Agree": 5, "Disagree": 3},
      "raw": { "...the untouched API object; only with --raw..." },
      "replies": [ { "...same fields, parent_comment_id set..." } ]
    }
  ]
}
```
