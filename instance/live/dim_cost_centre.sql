-- Declared shape of the cost-centre dimension.
--
-- Snapshot binding — no PARTITION BY; swap is EXCHANGE TABLES.
--
-- Raw projection of `master.cost_centres` on the customer's Postgres.
-- The PG column `cost_centre_id` is renamed to `cost_centre` at the
-- binding boundary — internal datamodel uses `cost_centre` everywhere.

(
    cost_centre        String,
    cost_centre_name   String,
    department         String,
    division           String,
    entity_id          String,
    is_billable        UInt8,
    _load_id           String,
    _ingested_at       DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY cost_centre