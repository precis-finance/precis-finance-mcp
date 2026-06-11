-- Declared shape of the timesheets fact.
--
-- Source: `hr.timesheets` on customer Postgres. Data is already at
-- monthly grain per (employee, project, cost_centre); the binding does
-- no aggregation, just identity projection + period filter.
--
-- `period` is the literal 'YYYY-MM' string derived from
-- period_start_date at the binding boundary. PARTITION BY period gives
-- per-period atomic REPLACE on the swap.

(
    period             String,
    period_start_date  Date,
    employee_id        Int32,
    project_id         Int32,
    cost_centre        String,
    hours_worked       Decimal(10, 2),
    hours_billable     Decimal(10, 2),
    activity_type      Nullable(String),
    _load_id           String,
    _ingested_at       DateTime DEFAULT now()
)
ENGINE = MergeTree
PARTITION BY period
ORDER BY (period, employee_id, project_id)
