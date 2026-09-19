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

Once the permanent Postgres exists, the simplest path is to skip the copy step entirely:
point `DATABASE_URL` at it and let the scraper write there directly. To move data that was
collected locally first:

```bash
docker exec foxcomments-db pg_dump -U foxcomments --data-only --table=articles \
  --table=authors --table=comments foxcomments | psql "$DATABASE_URL"
```

Run `python -m foxcomments db-init` against the new database first to create the tables.
Every table uses `ON CONFLICT` upserts, so re-importing the same rows is harmless.
