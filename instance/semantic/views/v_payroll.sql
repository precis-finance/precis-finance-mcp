-- Payroll fact view: one row per employee per period.
-- Source: live.fact_payroll.
-- Column names in landing strip the `_eur` suffix; renamed back at view
-- boundary for downstream catalogue compatibility.

SELECT
    p.employee_id    AS employee,
    p.cost_centre AS cost_centre,
    coalesce(cc.department, '') AS department,
    coalesce(cc.division, '')   AS division,
    coalesce(de.grade, '')      AS grade,
    formatDateTime(p.pay_date, '%Y-%m') AS period,
    coalesce(pd.quarter, '')     AS quarter,
    coalesce(pd.fiscal_year, '') AS fiscal_year,
    'ACTUALS'                    AS scenario,
    p.gross_salary             AS gross_salary_eur,
    p.employer_contributions   AS employer_contributions_eur,
    p.bonus                    AS bonus_eur,
    p.total_cost               AS total_cost_eur
FROM live.fact_payroll p
LEFT JOIN live.dim_cost_centre cc
    ON p.cost_centre = cc.cost_centre
LEFT JOIN live.dim_employee de
    ON p.employee_id = de.employee_id
LEFT JOIN live.dim_period pd
    ON formatDateTime(p.pay_date, '%Y-%m') = pd.period
