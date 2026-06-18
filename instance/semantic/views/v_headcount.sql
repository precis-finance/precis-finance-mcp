-- Headcount view: monthly active headcount and payroll cost by cost centre.
-- Source: live.fact_payroll.

WITH src AS (
    SELECT
        cost_centre,
        employee_id,
        formatDateTime(pay_date, '%Y-%m') AS period,
        gross_salary,
        employer_contributions,
        bonus,
        total_cost
    FROM live.fact_payroll
)
SELECT
    p.cost_centre AS cost_centre,
    p.period         AS period,
    coalesce(pd.quarter, '')     AS quarter,
    coalesce(pd.fiscal_year, '') AS fiscal_year,
    COUNT(DISTINCT p.employee_id)        AS headcount,
    SUM(p.gross_salary)                  AS total_gross_salary,
    SUM(p.employer_contributions)        AS total_employer_contributions,
    SUM(p.bonus)                         AS total_bonus,
    SUM(p.total_cost)                    AS total_payroll_cost
FROM src p
LEFT JOIN live.dim_period pd
    ON p.period = pd.period
GROUP BY p.cost_centre, p.period, pd.quarter, pd.fiscal_year
