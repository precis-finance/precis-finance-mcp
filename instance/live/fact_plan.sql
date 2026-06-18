-- Declared shape of the plan fact table (open tier).
--
-- Budget / forecast / what-if scenarios land here as plain period amounts,
-- one row per (period, account_code, cost_centre, scenario). This is the
-- open-tier analogue of the Précis planning.entries delta table — no
-- delta_amount, no commit_id, no versioning. A deployer lands a static plan
-- set (snapshot binding or direct seed) and the semantic views read it.
--
-- Kept a SEPARATE table from live.fact_gl on purpose. fact_gl is the
-- period-swap target for actuals (`ALTER TABLE … REPLACE PARTITION '<period>'`,
-- see precis_mcp/ingestion/swap.py), which replaces a whole period partition
-- atomically and assumes a single scenario per table. Co-locating plan
-- scenarios in fact_gl would let an actuals re-ingest wipe the plan rows for
-- that period. Plan therefore lives here, unpartitioned, loaded by snapshot
-- (EXCHANGE TABLES) or direct seed — never the period swap.
--
-- _load_id / _ingested_at are set by the ingestion runner, never by the
-- operator's extract query.

(
    period           String,
    account_code     String,
    cost_centre      String,
    scenario         String,
    amount           Decimal(18, 2),
    _load_id         String,
    _ingested_at     DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (scenario, period, account_code, cost_centre)
