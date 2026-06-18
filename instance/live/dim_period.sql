-- Declared shape of the period dimension.
--
-- Snapshot binding — no PARTITION BY; swap is EXCHANGE TABLES, which
-- swaps the full table atomically. ORDER BY is required by MergeTree.
--
-- Raw projection of `gl.dim_period` on the customer's Postgres. The
-- ragged calendar hierarchy (fiscal_year → quarter → period) is
-- derived in `semantic.dim_period_calendar` / `_rollup`, not here.

(
    period            String,
    quarter           String,
    fiscal_year       String,
    _load_id          String,
    _ingested_at      DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY period
