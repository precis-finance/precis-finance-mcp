-- Declared shape of the client dimension.
--
-- Snapshot binding — no PARTITION BY; swap is EXCHANGE TABLES.
-- Raw projection of `projects.clients` on the customer's Postgres.

(
    client_id      String,
    client_name    String,
    industry       Nullable(String),
    tier           Nullable(String),
    country        Nullable(String),
    created_date   Date,
    _load_id       String,
    _ingested_at   DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY client_id
