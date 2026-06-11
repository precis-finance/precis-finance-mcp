-- Declared shape of the CRM pipeline fact.
--
-- Source: `opportunities.csv` dropped under the `crm/` prefix of the
-- file-drop upload root. Read via DuckDB's `read_csv_auto`. Snapshot
-- binding — `EXCHANGE TABLES` replaces `live.fact_pipeline` atomically
-- on each load.

(
    opportunity_id            String,
    account_id                String,
    opportunity_name          Nullable(String),
    stage                     String,
    stage_category            String,
    probability               Decimal(5, 4),
    amount                    Decimal(14, 2),
    currency                  String,
    created_date              Date,
    close_date                Date,
    last_stage_change_date    Date,
    owner                     Nullable(String),
    service_line              Nullable(String),
    engagement_type           Nullable(String),
    duration_months           Nullable(Int32),
    estimated_start_date      Nullable(Date),
    source                    Nullable(String),
    _load_id                  String,
    _ingested_at              DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (close_date, opportunity_id)
