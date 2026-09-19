Full sequence on the Linux box:

# 1. User and code
sudo useradd --system --home /opt/foxcomments --shell /usr/sbin/nologin foxcomments
sudo git clone <your-repo> /opt/foxcomments
sudo chown -R foxcomments: /opt/foxcomments
cd /opt/foxcomments

# 2. Dependencies (Python 3.11+)
sudo -u foxcomments python3 -m venv .venv
sudo -u foxcomments .venv/bin/pip install -r requirements.txt

# 3. Config (config.toml is gitignored, so copy the example)
sudo -u foxcomments cp config.example.toml config.toml
sudo -u foxcomments mkdir -p logs
sudo nano config.toml          # keywords, daily_at, window

# 4. Database — remote:
printf 'DATABASE_URL=postgresql://user:pass@dbhost:5432/foxcomments\n' | sudo tee /etc/foxcomments.env
sudo chmod 600 /etc/foxcomments.env
#    or local, in Docker:  docker compose up -d --wait

# 5. Create the tables
sudo -u foxcomments .venv/bin/python -m foxcomments db-init

# 6. Start it
sudo cp deploy/foxcomments.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now foxcomments

Then check it:
systemctl status foxcomments
journalctl -u foxcomments -f
sudo -u foxcomments /opt/foxcomments/.venv/bin/python -m foxcomments status

Why systemd rather than cron: it restarts the service after a reboot or crash; on stop it sends SIGTERM, which makes the scraper put its in-flight article back in the queue instead of stranding it; and the service catches up a missed run if the box was off at 07:00, which cron won't do.

If you'd still rather use cron, the one-shot command works fine:
cron
0 7 * * * cd /opt/foxcomments && .venv/bin/python -m foxcomments daily

Three things to watch:
- Timezone: daily_at is local time and fresh servers are usually UTC. timedatectl set-timezone ..., or just set daily_at in UTC. Stored data is unaffected — it's all timestamptz.
- If you clone somewhere other than /opt/foxcomments, edit WorkingDirectory, ExecStart and ReadWritePaths in the unit file to match. ProtectSystem=strict means the service can't write outside ReadWritePaths, so a stale path there breaks logging.
- Running Postgres in Docker on that same box means the foxcomments user needs docker access. Installing the distro's postgresql package avoids that entirely, and is the simpler choice if the database is only temporary until your real one exists.

The first start runs discovery immediately (no prior run recorded), so you'll see it working right away rather than waiting until 07:00.
