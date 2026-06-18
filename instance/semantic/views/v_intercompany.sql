-- Intercompany recharge view — intra-group cross-charges (actuals only).
-- Joins the recharge fact to the cost-centre master for both the charged and
-- the counterparty cost centre. Not reconciled to the GL.

WITH cc_dim AS (
    SELECT cost_centre, cost_centre_name, department, division
    FROM live.dim_cost_centre
),
period_dim AS (
    SELECT period, quarter, fiscal_year
    FROM live.dim_period
)

SELECT
    concat(f.period, f.cost_centre, f.counterparty_cc) AS pk,
    'ENT-001' AS entity_id,
    f.cost_centre AS cost_centre,
    coalesce(cc.cost_centre_name, '') AS cost_centre_name,
    coalesce(cc.department, '')        AS department,
    coalesce(cc.division, '')          AS division,
    f.counterparty_cc AS counterparty_cc,
    coalesce(cp.cost_centre_name, '') AS counterparty_cc_name,
    coalesce(cp.department, '')         AS counterparty_department,
    coalesce(cp.division, '')           AS counterparty_division,
    f.period AS period,
    coalesce(pd.quarter, '')     AS quarter,
    coalesce(pd.fiscal_year, '') AS fiscal_year,
    'ACTUALS' AS scenario,
    '__actuals__' AS commit_id,
    SUM(f.amount) AS amount
FROM live.fact_intercompany f
LEFT JOIN cc_dim cc     ON f.cost_centre = cc.cost_centre
LEFT JOIN cc_dim cp     ON f.counterparty_cc = cp.cost_centre
LEFT JOIN period_dim pd ON f.period = pd.period
GROUP BY
    f.period, f.cost_centre, cc.cost_centre_name, cc.department, cc.division,
    f.counterparty_cc, cp.cost_centre_name, cp.department, cp.division,
    pd.quarter, pd.fiscal_year
