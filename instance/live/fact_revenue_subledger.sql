-- Declared shape of the revenue-subledger fact.
--
-- Source: `projects.revenue_subledger` on customer Postgres. One row
-- per (project, recognition_date); recognition_date is monthly.
-- Identity projection at the binding boundary.
--
-- Stock metrics (cum_*, wip_balance, percent_complete) are
-- cumulative at row level; the engine aggregates with
-- rollup_method='closing' across periods.

(
    period                   String,
    recognition_date         Date,
    project_id               Int32,
    cost_centre              String,
    client_id                String,
    project_type             String,
    recognition_method       String,
    currency                 String,
    hours_worked             Decimal(10, 2),
    hours_billable           Decimal(10, 2),
    revenue_recognised       Decimal(14, 2),
    cost_recognised          Decimal(14, 2),
    amount_billed            Decimal(14, 2),
    contract_value           Decimal(14, 2),
    percent_complete         Decimal(5, 4),
    cum_revenue_recognised   Decimal(14, 2),
    cum_cost_recognised      Decimal(14, 2),
    cum_billed               Decimal(14, 2),
    wip_balance              Decimal(14, 2),
    _load_id                 String,
    _ingested_at             DateTime DEFAULT now()
)
ENGINE = MergeTree
PARTITION BY period
ORDER BY (period, project_id)
