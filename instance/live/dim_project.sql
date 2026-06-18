-- Declared shape of the project dimension.
--
-- Snapshot binding — no PARTITION BY; swap is EXCHANGE TABLES.
-- Raw projection of `projects.projects` on the customer's Postgres.
-- Source column `cost_centre_id` is renamed to `cost_centre` at the
-- binding boundary.
--
-- `client_name` is a denormalised copy carried on PG; the semantic
-- `dim_project` view re-resolves it through dim_client so the canonical
-- name is sourced from one place, but we keep it here for audit /
-- direct-query convenience.

(
    project_id           Int32,
    project_code         String,
    project_name         Nullable(String),
    client_id            Nullable(String),
    client_name          Nullable(String),
    project_type         Nullable(String),
    status               Nullable(String),
    start_date           Nullable(Date),
    end_date             Nullable(Date),
    budget_hours         Nullable(Decimal(10, 1)),
    budget_revenue       Nullable(Decimal(14, 2)),
    contract_value       Nullable(Decimal(14, 2)),
    project_manager_id   Nullable(Int32),
    cost_centre          Nullable(String),
    currency             Nullable(String),
    _load_id             String,
    _ingested_at         DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY project_id
