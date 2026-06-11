-- Declared shape of the GL fact table.
--
-- Applied by the live-DDL runner to BOTH schemas with identical engine
-- spec — ClickHouse requires the source and target of `REPLACE PARTITION`
-- to share engine, PARTITION BY and ORDER BY exactly:
--
--     CREATE TABLE IF NOT EXISTS live.fact_gl    <body>
--     CREATE TABLE IF NOT EXISTS staging.fact_gl <body>
--
-- Period column is the literal accounting period (String 'YYYY-MM' or
-- 'YYYY-13' / 'YYYY-MM-ADJ' for adjustments). PARTITION BY period gives
-- per-period atomic REPLACE on the swap.
--
-- _load_id ties every row to its `load_history` row; _ingested_at is
-- wall-clock insert time. Both are set by the runner during the
-- Ibis-to-staging insert, never by the operator's extract query.

(
    period           String,
    account_code     String,
    cost_centre   String,
    amount           Decimal(18, 2),
    _load_id         String,
    _ingested_at     DateTime DEFAULT now()
)
ENGINE = MergeTree
PARTITION BY period
ORDER BY (period, account_code, cost_centre)