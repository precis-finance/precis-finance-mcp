-- Timesheets fact view: daily grain at (employee, project, cost_centre, day).
-- Source: live.fact_timesheets (one row per employee per business day, per
-- project + a non-billable row). `day`/`week`/`period` all derive cleanly from
-- the day leaf. `timesheet_id` is synthetic — landing carries no row id, so a
-- stable hash of the grain columns stands in for downstream uniqueness checks.

-- Hierarchy parents (department/division off cost_centre, grade off employee)
-- are resolved by the engine via a leaf-dimension join at query time, so they
-- are not denormalised here. Period parents (quarter/fiscal_year) stay.
SELECT
    cityHash64(t.period_start_date, t.employee_id, t.project_id) AS timesheet_id,
    t.employee_id    AS employee_id,
    t.project_id     AS project_id,
    t.cost_centre AS cost_centre,
    formatDateTime(t.period_start_date, '%Y-%m') AS period,
    formatDateTime(t.period_start_date, '%G-W%V') AS week,
    formatDateTime(t.period_start_date, '%Y-%m-%d') AS day,
    coalesce(pd.quarter, '')     AS quarter,
    coalesce(pd.fiscal_year, '') AS fiscal_year,
    'ACTUALS'                    AS scenario,
    t.activity_type,
    t.hours_worked,
    t.hours_billable,
    t.hours_worked - t.hours_billable AS hours_non_billable
FROM live.fact_timesheets t
LEFT JOIN live.dim_period pd
    ON formatDateTime(t.period_start_date, '%Y-%m') = pd.period
