-- Timesheets fact view: monthly aggregate at (employee, project, cost_centre).
-- Source: live.fact_timesheets (monthly grain). `timesheet_id` is
-- synthetic — landing doesn't carry a row id, so a stable hash of the grain
-- columns stands in for downstream uniqueness checks.

SELECT
    cityHash64(t.period_start_date, t.employee_id, t.project_id) AS timesheet_id,
    t.employee_id    AS employee,
    t.project_id     AS project,
    t.cost_centre AS cost_centre,
    coalesce(cc.department, '') AS department,
    coalesce(cc.division, '')   AS division,
    coalesce(de.grade, '')      AS grade,
    formatDateTime(t.period_start_date, '%Y-%m') AS period,
    coalesce(pd.quarter, '')     AS quarter,
    coalesce(pd.fiscal_year, '') AS fiscal_year,
    'ACTUALS'                    AS scenario,
    t.activity_type,
    t.hours_worked,
    t.hours_billable,
    t.hours_worked - t.hours_billable AS hours_non_billable
FROM live.fact_timesheets t
LEFT JOIN live.dim_cost_centre cc
    ON t.cost_centre = cc.cost_centre
LEFT JOIN live.dim_employee de
    ON t.employee_id = de.employee_id
LEFT JOIN live.dim_period pd
    ON formatDateTime(t.period_start_date, '%Y-%m') = pd.period
