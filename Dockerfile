FROM python:3.13-slim

# tzdata so TZ= in docker-compose.yml makes `daily_at` mean local time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY foxcomments/ ./foxcomments/
COPY config.example.toml .

# Config lives on a mounted volume; without one the built-in defaults apply.
ENV FOXCOMMENTS_CONFIG=/app/config/config.toml \
    PYTHONUNBUFFERED=1

RUN useradd --system --uid 1000 --create-home app \
    && mkdir -p /app/logs /app/config \
    && chown -R app:app /app
USER app

ENTRYPOINT ["python", "-m", "foxcomments"]
CMD ["service"]
