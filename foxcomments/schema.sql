-- Idempotent: safe to run on every start.

-- One row per discovered article. Doubles as the scrape queue via `status`.
CREATE TABLE IF NOT EXISTS articles (
    id                BIGSERIAL PRIMARY KEY,
    url               TEXT        NOT NULL UNIQUE,
    title             TEXT        NOT NULL,
    published_at      TIMESTAMPTZ,
    source            TEXT,                        -- e.g. 'feed:politics', 'search'
    matched_keywords  TEXT[]      NOT NULL DEFAULT '{}',
    comment_thread_id UUID,                        -- hedgehog embed id
    status            TEXT        NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'in_progress', 'done', 'failed', 'no_comments')),
    attempts          INT         NOT NULL DEFAULT 0,
    last_error        TEXT,
    discovered_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    scrape_after      TIMESTAMPTZ NOT NULL DEFAULT now(),   -- let the article gather comments first
    last_attempt_at   TIMESTAMPTZ,
    scraped_at        TIMESTAMPTZ,
    rescrape_at       TIMESTAMPTZ,                 -- next re-scrape of a done article for new comments
    top_level_count   INT,
    reply_count       INT
);
ALTER TABLE articles ADD COLUMN IF NOT EXISTS scrape_after TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE articles ADD COLUMN IF NOT EXISTS rescrape_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS articles_queue_idx ON articles (status, scrape_after, discovered_at);
CREATE INDEX IF NOT EXISTS articles_published_idx ON articles (published_at);
-- Watermark for downstream ETL: "everything written since my last pull".
CREATE INDEX IF NOT EXISTS articles_scraped_idx ON articles (scraped_at);

CREATE TABLE IF NOT EXISTS authors (
    id            TEXT PRIMARY KEY,
    username      TEXT,
    display_name  TEXT,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS authors_last_seen_idx ON authors (last_seen_at);

CREATE TABLE IF NOT EXISTS comments (
    id                TEXT PRIMARY KEY,
    article_id        BIGINT      NOT NULL REFERENCES articles (id) ON DELETE CASCADE,
    parent_comment_id TEXT,                        -- NULL for top-level comments
    author_id         TEXT        REFERENCES authors (id),
    body              TEXT,
    created_at        TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ,
    edited            BOOLEAN,
    deleted           BOOLEAN,
    pinned            BOOLEAN,
    score             DOUBLE PRECISION,
    reaction_total    INT         NOT NULL DEFAULT 0,
    reactions         JSONB       NOT NULL DEFAULT '{}',
    agree_count       INT GENERATED ALWAYS AS (COALESCE((reactions ->> 'Agree')::INT, 0)) STORED,
    disagree_count    INT GENERATED ALWAYS AS (COALESCE((reactions ->> 'Disagree')::INT, 0)) STORED,
    images            JSONB       NOT NULL DEFAULT '[]',
    videos            JSONB       NOT NULL DEFAULT '[]',
    raw               JSONB,                       -- the untouched API object, for fields we don't model
    scraped_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE comments ADD COLUMN IF NOT EXISTS raw JSONB;
CREATE INDEX IF NOT EXISTS comments_article_idx ON comments (article_id);
CREATE INDEX IF NOT EXISTS comments_author_idx ON comments (author_id);
CREATE INDEX IF NOT EXISTS comments_parent_idx ON comments (parent_comment_id);
CREATE INDEX IF NOT EXISTS comments_created_idx ON comments (created_at);
CREATE INDEX IF NOT EXISTS comments_scraped_idx ON comments (scraped_at, id);

-- One row per discover/work/daily invocation, for monitoring.
CREATE TABLE IF NOT EXISTS runs (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT        NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    stats       JSONB       NOT NULL DEFAULT '{}',
    error       TEXT
);
