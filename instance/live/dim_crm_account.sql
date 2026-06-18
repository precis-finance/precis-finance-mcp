-- Declared shape of the CRM account dimension.
--
-- Source: `accounts.csv` dropped under the `crm/` prefix of the
-- file-drop upload root. Read via DuckDB's `read_csv_auto`. Snapshot
-- binding.

(
    account_id      String,
    account_name    String,
    industry        Nullable(String),
    region          Nullable(String),
    segment         Nullable(String),
    created_date    Date,
    _load_id        String,
    _ingested_at    DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY account_id
