-- General Ledger view: all financial accounts (P&L + Balance Sheet).
-- Excludes statistical accounts (9xxx series: hours, FTEs).
-- Used by the GL domain for account-level drill-down.
--
-- Sources:
--   - Actuals from live.fact_gl (ingested via customer_pg__gl).
--     Landing dataset is minimal — recovers account_name/account_type/fs_line
--     by joining to the canonical live.dim_account; period is carried
--     directly off the landing row (the accounting period, not a derived
--     calendar month — adjustment / 13th periods flow through unchanged);
--     entity_id is a literal (single-entity vertical).
--   - Plan / Budget / Forecast from live.fact_plan (static open-tier snapshots).

WITH account_dim AS (
    SELECT
        account_code,
        account_name,
        account_type,
        fs_line
    FROM live.dim_account
),

-- Cost-centre hierarchy parents (department/division) are resolved by the
-- engine via a leaf-dimension join at query time, so they are not denormalised
-- here. account_type/fs_line stay: they are referenced by metric `where:`.
period_dim AS (
    SELECT period, quarter, fiscal_year
    FROM live.dim_period
),

actuals AS (
    SELECT
        'ENT-001'        AS entity_id,
        a.account_code,
        a.cost_centre,
        a.period,
        a.amount
    FROM live.fact_gl a
)

-- Actuals: all financial accounts (exclude statistical 9xxx)
SELECT
    a.entity_id,
    a.account_code AS account,
    coalesce(ad.account_name, 'Unknown') AS account_name,
    coalesce(ad.account_type, 'Unknown') AS account_type,
    coalesce(ad.fs_line, 'Unknown')      AS fs_line,
    a.cost_centre AS cost_centre,
    a.period AS period,
    coalesce(pd.quarter, '')     AS quarter,
    coalesce(pd.fiscal_year, '') AS fiscal_year,
    'ACTUALS' AS scenario,
    '__actuals__' AS commit_id,
    SUM(a.amount) AS amount
FROM actuals a
LEFT JOIN account_dim ad ON a.account_code = ad.account_code
LEFT JOIN period_dim pd  ON a.period = pd.period
WHERE a.account_code NOT LIKE '9%'
GROUP BY a.entity_id, a.account_code, ad.account_name, ad.account_type, ad.fs_line, a.cost_centre, a.period, pd.quarter, pd.fiscal_year

UNION ALL

-- Plan / Budget / Forecast: static open-tier snapshots (exclude statistical 9xxx)
SELECT
    'ENT-001' AS entity_id,
    e.account_code,
    coalesce(ad.account_name, 'Unknown') AS account_name,
    coalesce(ad.account_type, 'Unknown') AS account_type,
    coalesce(ad.fs_line, 'Unknown')      AS fs_line,
    e.cost_centre,
    e.period AS period,
    coalesce(pd.quarter, '')     AS quarter,
    coalesce(pd.fiscal_year, '') AS fiscal_year,
    e.scenario,
    '__plan__' AS commit_id,
    SUM(e.amount) AS amount
FROM live.fact_plan e
LEFT JOIN account_dim ad ON e.account_code = ad.account_code
LEFT JOIN period_dim pd  ON e.period = pd.period
WHERE e.scenario != 'ACTUALS'
  AND e.account_code NOT LIKE '9%'
GROUP BY e.account_code, coalesce(ad.account_name, 'Unknown'), coalesce(ad.account_type, 'Unknown'), coalesce(ad.fs_line, 'Unknown'), e.cost_centre, e.period, pd.quarter, pd.fiscal_year, e.scenario
