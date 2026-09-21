# Deploying on a headless Linux host

Two ways to run this. Docker is fewer steps and brings its own Python and Postgres;
the systemd route suits a host that already has Postgres or doesn't run Docker.

# Option A: Docker (fewest steps)

Needs Docker Engine with the compose plugin.

```bash
git clone <your-repo> foxcomments && cd foxcomments

cp .env.example .env                        # set TZ and a Postgres password
mkdir -p config logs                        # create these yourself: see Logs below
cp config.example.toml config/config.toml
nano config/config.toml                     # keywords, daily_at, window

docker compose up -d                        # builds the image, starts Postgres + scraper
```

That's it. The scraper creates its tables on first start, runs discovery straight away, then
settles into the daily schedule. `restart: unless-stopped` brings both containers back after
a reboot, so nothing else needs configuring.

Day to day:

```bash
docker compose logs -f scraper              # live log
docker compose run --rm scraper status      # queue summary
docker compose run --rm scraper discover --dry-run
docker compose down                         # stop; data survives in the named volume
git pull && docker compose up -d --build    # update
```

`docker compose run --rm scraper <anything>` works for every command in the CLI, since the
image's entrypoint is `python -m foxcomments`.

**Settings live in two files.** `.env` holds the Postgres password, `TZ`, and optionally
`DATABASE_URL`; compose reads it automatically. `config/config.toml` holds the scraping
settings and is mounted read-only into the container. Editing it needs only a
`docker compose restart scraper`. With no `config/config.toml` at all the built-in defaults
apply, so the stack still starts.

Pass environment variables through `.env` rather than the shell. Compose reads `.env`
reliably, whereas `VAR=x docker compose up` depends on the shell (a value containing a slash
silently fails to reach the container under Git Bash on Windows, for instance).

### Using a database elsewhere

Set `DATABASE_URL` in `.env` and start only the scraper, leaving the bundled Postgres out:

```bash
echo 'DATABASE_URL=postgresql://user:password@dbhost:5432/foxcomments?sslmode=require' >> .env
docker compose up -d scraper
```

### Port 5432 already in use

Another Postgres on the host already owns the port, and compose refuses to start:

```
Bind for 0.0.0.0:5432 failed: port is already allocated
```

Publish this one somewhere else in `.env` and bring the stack back up:

```bash
echo 'POSTGRES_PORT=5433' >> .env
docker compose up -d
```

Only the host-side port moves: the scraper reaches Postgres at `db:5432` over the compose
network either way, so nothing else changes. From the host it is now
`psql -h 127.0.0.1 -p 5433 -U foxcomments foxcomments`. `sudo ss -ltnp | grep 5432` names
whatever holds the original port.

### Timezone

`daily_at` is local time, and containers default to UTC. `TZ` in `.env` sets it:

```bash
TZ=America/New_York
```

Confirm with `docker compose run --rm scraper status`, whose log lines are stamped in local
time. Only the schedule is affected; stored data is `timestamptz` either way.

### Logs

The journal equivalent is `docker compose logs`. Daily files also land in `./logs`, which is
mounted into the container.

Create `./logs` before the first `docker compose up`. Docker creates a missing bind-mount
source as `root`, and the container runs as an unprivileged user (uid 1000), which then
cannot write there. The scraper warns and logs to the console only, so `docker compose logs`
still works, but the daily files stay empty. To repair it afterwards:

```bash
mkdir -p logs && sudo chown 1000:1000 logs
docker compose restart scraper
```

Adjust the uid if you changed the `useradd` line in the `Dockerfile`. Cap the container's own log growth if the host is long-lived, by
adding to the `scraper` service:

```yaml
    logging:
      driver: json-file
      options: {max-size: "10m", max-file: "5"}
```

# Option B: systemd and a virtualenv

Nothing here is Windows-specific; the scheduler is plain Python.

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
printf 'DATABASE_URL=postgresql://foxcomments:PASSWORD@dbhost:5432/foxcomments?sslmode=require
'   | sudo tee /etc/foxcomments.env
sudo chmod 600 /etc/foxcomments.env
```

`DATABASE_URL` overrides `config.toml`. Tables are created on first run, so a fresh
empty database is all that's needed. Drop `?sslmode=require` if the server has no TLS
(fine over a trusted LAN, but prefer TLS or an SSH tunnel otherwise).

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

Cloned somewhere other than `/opt/foxcomments`? Update `WorkingDirectory`, `ExecStart` and
`ReadWritePaths` in the unit file: `ProtectSystem=strict` blocks writes outside the paths
listed there.

`daily_at` is **local** time, and a fresh server usually runs on UTC:

```bash
sudo timedatectl set-timezone America/New_York   # or leave UTC and set daily_at to match
```

### Prefer cron or a systemd timer?

`python -m foxcomments daily` is a one-shot run that exits when finished, so the classic
approach works too:

```cron
0 7 * * * cd /opt/foxcomments && .venv/bin/python -m foxcomments daily
```

It gives up the things the service does for you: catching up a run missed while the machine
was off, and releasing the in-flight article on shutdown instead of waiting for the stale
sweep.

### Logs

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

# Both options

Concurrent runs are safe: articles are claimed with `FOR UPDATE SKIP LOCKED`, so two
processes never scrape the same article, and re-discovering an article is a no-op. The
service runs at most once per scheduled slot, recorded in the `runs` table, and catches up a
slot missed while the machine was off. On SIGTERM it returns the in-flight article to the
queue before exiting.

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

`comments.raw` holds each comment's complete API object, including the ~20 fields the
columns don't model (`flagged`, `sensitiveMaterial`, `quoted`, `threadParent`,
`scoreComputed`, `links` and friends), so a later ETL can mine something this schema never
anticipated. Articles are scraped once, so anything not captured at scrape time is gone for
good; that column is the insurance. Set `store_raw = false` under `[scraper]` to skip it and
roughly halve the storage.

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
