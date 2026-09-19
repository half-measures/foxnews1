# Deploying on a headless Linux host

Nothing here is Windows-specific; the scheduler is plain Python and runs under systemd.

## 1. Install

```bash
sudo useradd --system --home /opt/foxcomments --shell /usr/sbin/nologin foxcomments
sudo mkdir -p /opt/foxcomments && sudo chown foxcomments: /opt/foxcomments

sudo -u foxcomments git clone <your-repo> /opt/foxcomments
cd /opt/foxcomments
sudo -u foxcomments python3 -m venv .venv
sudo -u foxcomments .venv/bin/pip install -r requirements.txt
sudo -u foxcomments cp config.example.toml config.toml   # edit keywords, schedule
sudo -u foxcomments mkdir -p logs
```

Python 3.11 or newer is required (the config loader uses `tomllib`).

## 2. Point it at a database

Put the connection string in an environment file rather than `config.toml`, so the
password isn't in the repo:

```bash
printf 'DATABASE_URL=postgresql://foxcomments:PASSWORD@dbhost:5432/foxcomments?sslmode=require\n' \
  | sudo tee /etc/foxcomments.env
sudo chmod 600 /etc/foxcomments.env
```

`DATABASE_URL` overrides `config.toml`. Tables are created on first run, so a fresh
empty database is all that's needed. Drop `?sslmode=require` if the server has no TLS
(fine over a trusted LAN, but prefer TLS or an SSH tunnel otherwise).

Running Postgres on the same box instead? `docker compose up -d` still works, or use the
distribution's `postgresql` package and point `DATABASE_URL` at `127.0.0.1`.

## 3. Run it as a service

```bash
sudo cp deploy/foxcomments.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now foxcomments
```

Check on it:

```bash
systemctl status foxcomments
journalctl -u foxcomments -f            # live log
sudo -u foxcomments /opt/foxcomments/.venv/bin/python -m foxcomments status
```

The service sleeps until `daily_at` (07:00 by default), runs discovery, then scrapes the
queue across the worker window, and sleeps again. It wakes hourly, so a machine that was
off at 07:00 catches up as soon as it comes back rather than skipping the day. Each run is
recorded in the `runs` table, so it runs at most once per scheduled slot.

`systemctl stop` sends SIGTERM, and the scraper puts the in-flight article back in the queue
before exiting. No article is stranded by a restart or reboot.

### Prefer cron or a systemd timer?

`python -m foxcomments daily` is a one-shot run that exits when finished, so the classic
approach works too:

```cron
0 7 * * * cd /opt/foxcomments && .venv/bin/python -m foxcomments daily
```

Concurrent runs are safe either way: articles are claimed with `FOR UPDATE SKIP LOCKED`,
so two processes never scrape the same article, and re-discovering an article is a no-op.

## 4. Logs

Logs go to the journal and to `logs/foxcomments-YYYY-MM-DD.log`. The files are small
(a few KB per run) but nothing deletes them, so add a logrotate rule if the host is
long-lived:

```
/opt/foxcomments/logs/*.log {
    weekly
    rotate 12
    compress
    missingok
    notifempty
}
```

Set `dir = ""` under `[logging]` in `config.toml` to use the journal alone.

## Reaching the database from elsewhere (ETL)

`docker-compose.yml` publishes Postgres on `127.0.0.1:5432`, which only restricts access
from *other machines*:

- **Another container** on the same compose project reaches it at `db:5432` regardless of
  that binding. Add your ETL service to `docker-compose.yml` and it just works.
- **Another process on the same host** connects to `127.0.0.1:5432` normally.
- **Another machine** is blocked until you publish the port more widely
  (`"0.0.0.0:5432:5432"`, or a specific LAN address) and set a real password in `.env`.
  An SSH tunnel avoids exposing the port at all:
  `ssh -L 5432:127.0.0.1:5432 user@scraper-host`.

To move data collected here into a database elsewhere in one shot:

```bash
docker exec foxcomments-db pg_dump -U foxcomments --data-only --table=articles \
  --table=authors --table=comments foxcomments | psql "$DATABASE_URL"
```

Run `python -m foxcomments db-init` against the destination first to create the tables.
Every table uses `ON CONFLICT` upserts, so re-importing the same rows is harmless.

# Reading the raw tables from an ETL program

These tables are the raw landing zone: comment bodies are stored verbatim, reactions and
attachments keep their original JSON, and nothing is normalized, deduplicated or
interpreted. A separate program can treat them as the source of truth and build whatever it
likes downstream, re-deriving everything from scratch whenever it wants.

Guarantees this side of the line upholds:

- **Rows are never deleted or renumbered.** The scraper only inserts and upserts, so there
  are no tombstones to handle and no ids that get reused. (`comments.deleted` records that
  Fox flagged a comment, not that the row went away.)
- **Articles land whole.** Every comment and reply for an article is written in one
  transaction together with the article's `done` status, so a reader never sees a partial
  article, even mid-scrape.
- **Primary keys are stable and come from Fox.** `comments.id` and `authors.id` are Fox's
  own ids, so re-pulling the same row is always recognizable as the same row.

## Incremental pulls

Every table carries a watermark column, indexed for this purpose:

| Table | Watermark | Moves when |
|---|---|---|
| `comments` | `scraped_at` | The comment is first stored, or re-stored with new text/reactions |
| `articles` | `scraped_at` | The article finishes scraping |
| `authors` | `last_seen_at` | The author is seen on any newly stored comment |

A pull looks like this, keeping the largest `scraped_at` it has seen:

```sql
SELECT c.*, a.url, a.title, a.published_at
FROM comments c
JOIN articles a ON a.id = c.article_id
WHERE c.scraped_at > :watermark - interval '5 minutes'
ORDER BY c.scraped_at, c.id
LIMIT 50000;
```

Two details worth getting right:

- **Overlap the window, don't trust equality.** `scraped_at` is the transaction's start
  time, and a transaction becomes visible slightly after that, so a row can appear with a
  timestamp just below a watermark you already recorded. Re-reading the last few minutes
  each time costs little and closes that gap. Upserting on `comments.id` downstream makes
  the repeats harmless.
- **Page by `(scraped_at, id)`**, which matches the `comments_scraped_idx` index, so paging
  stays fast as the table grows.

For a full rebuild, drop the `WHERE` clause and page through everything, or take a
`pg_dump` snapshot.

## A read-only account for it

Give the ETL its own login so it cannot modify the raw data:

```sql
CREATE ROLE etl LOGIN PASSWORD 'choose-something-long';
GRANT CONNECT ON DATABASE foxcomments TO etl;
GRANT USAGE ON SCHEMA public TO etl;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO etl;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO etl;
```

Run it with `docker exec -it foxcomments-db psql -U foxcomments`. The last line covers
tables added later.

## Backups

The raw tables are only a safe copy if the database itself is backed up. A nightly dump,
kept off the scraper host:

```bash
docker exec foxcomments-db pg_dump -U foxcomments -Fc foxcomments \
  > /backup/foxcomments-$(date +%F).dump
```

Restore with `pg_restore -d foxcomments foxcomments-YYYY-MM-DD.dump`. If the ETL program
writes the extracted data somewhere durable anyway, that is a second copy, but it is a
*derived* one: only the dump brings back the raw tables exactly as they were.
