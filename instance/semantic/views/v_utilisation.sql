-- Utilisation view: billable vs available hours by employee, cost centre, period.
-- Source: live.fact_timesheets (monthly grain).

WITH src AS (
    SELECT
        employee_id,
        cost_centre,
        formatDateTime(period_start_date, '%Y-%m') AS period,
        hours_worked,
        hours_billable
    FROM live.fact_timesheets
)
SELECT
    t.employee_id,
    t.cost_centre AS cost_centre,
    t.period         AS period,
    coalesce(pd.quarter, '')     AS quarter,
    coalesce(pd.fiscal_year, '') AS fiscal_year,
    SUM(t.hours_worked)                                           AS total_hours,
    SUM(t.hours_billable)                                         AS billable_hours,
    SUM(t.hours_worked) - SUM(t.hours_billable)                   AS non_billable_hours,
    SUM(t.hours_billable) / nullIf(SUM(t.hours_worked), 0)        AS utilisation_rate
FROM src t
LEFT JOIN live.dim_period pd
    ON t.period = pd.period
GROUP BY t.employee_id, t.cost_centre, t.period, pd.quarter, pd.fiscal_year
