-- Declared shape of the account dimension.
--
-- Snapshot binding — no PARTITION BY; swap is EXCHANGE TABLES, which
-- swaps the full table atomically. ORDER BY is required by MergeTree.
--
-- Raw projection of `gl.accounts` on the customer's Postgres. The
-- presentation filter (is_active = TRUE AND account_type != 'HEADER')
-- lives in `semantic.dim_account`, not here — keeps `live.dim_account`
-- queryable for audit.

(
    account_code      String,
    account_name      String,
    account_type      String,
    fs_line           String,
    normal_balance    String,
    parent_code       Nullable(String),
    is_active         UInt8,
    _load_id          String,
    _ingested_at      DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY account_code