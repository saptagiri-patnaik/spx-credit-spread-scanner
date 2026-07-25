-- Reference schema for the SPX scanner (mirrors db/models.py).
-- You can run `python main.py --setup` instead, which calls create_all().

CREATE TABLE IF NOT EXISTS items (
    id            BIGSERIAL PRIMARY KEY,
    source        VARCHAR(160) NOT NULL,
    source_type   VARCHAR(30)  NOT NULL,
    external_id   VARCHAR(512) NOT NULL,
    content_hash  VARCHAR(64)  NOT NULL UNIQUE,
    url           TEXT,
    title         TEXT,
    content       TEXT,
    author        VARCHAR(255),
    category      VARCHAR(60),
    region        VARCHAR(60),
    engagement    DOUBLE PRECISION DEFAULT 0,
    published_at  TIMESTAMPTZ,
    fetched_at    TIMESTAMPTZ DEFAULT now(),
    scored        BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS ix_items_source_type ON items (source_type);
CREATE INDEX IF NOT EXISTS ix_items_scored ON items (scored);

CREATE TABLE IF NOT EXISTS item_scores (
    id           BIGSERIAL PRIMARY KEY,
    item_id      BIGINT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    direction    DOUBLE PRECISION NOT NULL,
    magnitude    DOUBLE PRECISION NOT NULL,
    confidence   DOUBLE PRECISION NOT NULL,
    macro_impact VARCHAR(20),
    catalysts    JSONB,
    model        VARCHAR(80) NOT NULL,
    scored_at    TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_item_scores_item ON item_scores (item_id);

CREATE TABLE IF NOT EXISTS predictions (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ DEFAULT now(),
    horizon_days    INTEGER NOT NULL,
    direction       DOUBLE PRECISION NOT NULL,
    label           VARCHAR(20) NOT NULL,
    confidence      DOUBLE PRECISION NOT NULL,
    sentiment_score DOUBLE PRECISION NOT NULL,
    macro_score     DOUBLE PRECISION NOT NULL,
    event_risk      BOOLEAN DEFAULT FALSE,
    market_context  JSONB,
    num_new_items   INTEGER DEFAULT 0,
    rationale       TEXT
);

CREATE TABLE IF NOT EXISTS spread_suggestions (
    id             BIGSERIAL PRIMARY KEY,
    prediction_id  BIGINT NOT NULL REFERENCES predictions(id) ON DELETE CASCADE,
    created_at     TIMESTAMPTZ DEFAULT now(),
    underlying     VARCHAR(20) NOT NULL,
    strategy       VARCHAR(40) NOT NULL,
    short_strike   DOUBLE PRECISION NOT NULL,
    long_strike    DOUBLE PRECISION NOT NULL,
    expiration     VARCHAR(20) NOT NULL,
    dte            INTEGER NOT NULL,
    width          DOUBLE PRECISION NOT NULL,
    credit         DOUBLE PRECISION NOT NULL,
    max_loss       DOUBLE PRECISION NOT NULL,
    pop            DOUBLE PRECISION NOT NULL,
    short_delta    DOUBLE PRECISION NOT NULL,
    expected_move  DOUBLE PRECISION NOT NULL,
    ror            DOUBLE PRECISION NOT NULL DEFAULT 0,
    edge           DOUBLE PRECISION NOT NULL DEFAULT 0,
    buffer         DOUBLE PRECISION NOT NULL DEFAULT 0,
    breakeven      DOUBLE PRECISION,
    notes          TEXT
);
CREATE INDEX IF NOT EXISTS ix_spreads_prediction ON spread_suggestions (prediction_id);

CREATE TABLE IF NOT EXISTS api_usage (
    id          BIGSERIAL PRIMARY KEY,
    provider    VARCHAR(30) NOT NULL,
    day         DATE NOT NULL,
    posts_read  INTEGER DEFAULT 0,
    cost        DOUBLE PRECISION DEFAULT 0,
    CONSTRAINT uq_api_usage_provider_day UNIQUE (provider, day)
);
CREATE INDEX IF NOT EXISTS ix_api_usage_provider ON api_usage (provider);
CREATE INDEX IF NOT EXISTS ix_api_usage_day ON api_usage (day);

CREATE TABLE IF NOT EXISTS collector_state (
    key        VARCHAR(80) PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ DEFAULT now()
);
