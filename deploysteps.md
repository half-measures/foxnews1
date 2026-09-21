Deploying on the Linux box:

git clone <your-repo> foxcomments && cd foxcomments
cp .env.example .env                                   # TZ + Postgres password
mkdir -p config && cp config.example.toml config/config.toml   # keywords, daily_at
docker compose up -d

That builds the image and starts Postgres and the scraper. Tables are created on first start, discovery runs immediately, then it settles into the daily schedule. Both containers are restart: unless-stopped, so a reboot needs nothing from you — no system user, no venv, no systemd unit, no Python version to worry about.

Day to day:
docker compose logs -f scraper           # live log
docker compose run --rm scraper status   # queue summary
git pull && docker compose up -d --build # update
Any CLI command works after run --rm scraper, since the entrypoint is python -m foxcomments.
docker compose logs -f scraper           # live log
docker compose run --rm scraper status   # queue summary
git pull && docker compose up -d --build # update
Any CLI command works after run --rm scraper, since the entrypoint is python -m foxcomments.
Three things to watch:
- Timezone: daily_at is local time and fresh servers are usually UTC. timedatectl set-timezone ..., or just set daily_at in UTC. Stored data is unaffected — it's all timestamptz.
- If you clone somewhere other than /opt/foxcomments, edit WorkingDirectory, ExecStart and ReadWritePaths in the unit file to match. ProtectSystem=strict means the service can't write outside ReadWritePaths, so a stale path there breaks logging.
- Running Postgres in Docker on that same box means the foxcomments user needs docker access. Installing the distro's postgresql package avoids that entirely, and is the simpler choice if the database is only temporary until your real one exists.

The first start runs discovery immediately (no prior run recorded), so you'll see it working right away rather than waiting until 07:00.
