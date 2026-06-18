-- P&L view: unifies actuals (from live.fact_gl) and plan
-- (from live.fact_plan) plus four statistical-account sections
-- sourced from live.fact_timesheets + live.fact_payroll.
--
-- OPEN TIER variant: identical to the Précis view except the plan leg
-- reads scenarios from live.fact_plan (static amounts) instead of the
-- Précis planning.entries delta table.
--
-- Statistical accounts (9xxx) carry non-currency quantities — hours
-- and FTEs — that need P&L-style rollup semantics so they appear
-- alongside revenue / cost in summary reports:
--   9100 — Billable Hours  (SUM hours_billable from timesheets)
--   9110 — Total Hours     (SUM hours_worked   from timesheets)
--   9200 — FTEs Billable   (COUNT DISTINCT employee_id from payroll,
--                           cost centres with is_billable = 1)
--   9210 — FTEs Overhead   (COUNT DISTINCT employee_id from payroll,
--                           cost centres with is_billable = 0)
--
-- Shape: each source aggregates to the canonical
-- (entity_id, account, cost_centre, period, scenario, commit_id) grain
-- and projects into a uniform column list inside the `unified` CTE.
-- The union assembles all six sources at that grain; the outer SELECT
-- joins the three dim tables (account / cost_centre / period) once,
-- not per-source.
--
-- rollup_method: controls how values aggregate across periods in summary views
--   'sum'     — standard: SUM(amount) across selected periods
--   'avg'     — period average: SUM(amount) / COUNT(DISTINCT period)
--   'closing' — last period only: value WHERE period = MAX(period)

WITH unified AS (
    -- Actuals (GL) — P&L + BS, BS excluded post-join via fs_line lookup
    SELECT
        'ENT-001'      AS entity_id,
        account_code   AS account,
        cost_centre    AS cost_centre,
        period         AS period,
        'ACTUALS'      AS scenario,
        SUM(amount)    AS amount,
        'sum'          AS rollup_method,
        '__actuals__'  AS commit_id,
        CAST(NULL AS Nullable(String)) AS hardcoded_account_name,
        CAST(NULL AS Nullable(String)) AS hardcoded_account_type,
        CAST(NULL AS Nullable(String)) AS hardcoded_fs_line
    FROM live.fact_gl
    GROUP BY account_code, cost_centre, period

    UNION ALL

    -- Plan / Budget / Forecast scenarios — open tier: from live.fact_plan
    SELECT
        'ENT-001'                   AS entity_id,
        p.account_code              AS account,
        p.cost_centre               AS cost_centre,
        p.period                    AS period,
        p.scenario                  AS scenario,
        SUM(p.amount)               AS amount,
        CASE WHEN p.account_code IN ('9200', '9210') THEN 'avg'
             ELSE 'sum'
        END                         AS rollup_method,
        '__plan__'                  AS commit_id,
        CAST(NULL AS Nullable(String)) AS hardcoded_account_name,
        CAST(NULL AS Nullable(String)) AS hardcoded_account_type,
        CAST(NULL AS Nullable(String)) AS hardcoded_fs_line
    FROM live.fact_plan p
    WHERE p.scenario != 'ACTUALS'
    GROUP BY p.account_code, p.cost_centre, p.period, p.scenario

    UNION ALL

    -- Statistical: Billable Hours (account 9100, ACTUALS only)
    SELECT
        'ENT-001'             AS entity_id,
        '9100'                AS account,
        cost_centre           AS cost_centre,
        period                AS period,
        'ACTUALS'             AS scenario,
        SUM(hours_billable)   AS amount,
        'sum'                 AS rollup_method,
        '__actuals__'         AS commit_id,
        'Billable Hours'      AS hardcoded_account_name,
        'STATISTICAL'         AS hardcoded_account_type,
        'Statistical'         AS hardcoded_fs_line
    FROM live.fact_timesheets
    GROUP BY cost_centre, period

    UNION ALL

    -- Statistical: Total Hours (account 9110, ACTUALS only)
    SELECT
        'ENT-001'             AS entity_id,
        '9110'                AS account,
        cost_centre           AS cost_centre,
        period                AS period,
        'ACTUALS'             AS scenario,
        SUM(hours_worked)     AS amount,
        'sum'                 AS rollup_method,
        '__actuals__'         AS commit_id,
        'Total Hours'         AS hardcoded_account_name,
        'STATISTICAL'         AS hardcoded_account_type,
        'Statistical'         AS hardcoded_fs_line
    FROM live.fact_timesheets
    GROUP BY cost_centre, period

    UNION ALL

    -- Statistical: FTEs — billable + overhead in one block, account
    -- code switched by cc.is_billable. The cc join must stay
    -- source-side here because is_billable steers the row's account.
    SELECT
        'ENT-001'                                                                       AS entity_id,
        CASE WHEN cc.is_billable = 1 THEN '9200' ELSE '9210' END                        AS account,
        p.cost_centre                                                                   AS cost_centre,
        p.period                                                                        AS period,
        'ACTUALS'                                                                       AS scenario,
        CAST(COUNT(DISTINCT p.employee_id) AS Decimal(18, 2))                           AS amount,
        'avg'                                                                           AS rollup_method,
        '__actuals__'                                                                   AS commit_id,
        CASE WHEN cc.is_billable = 1 THEN 'FTEs - Billable' ELSE 'FTEs - Overhead' END  AS hardcoded_account_name,
        'STATISTICAL'                                                                   AS hardcoded_account_type,
        'Statistical'                                                                   AS hardcoded_fs_line
    FROM live.fact_payroll p
    INNER JOIN live.dim_cost_centre cc ON p.cost_centre = cc.cost_centre
    GROUP BY p.cost_centre, p.period, cc.is_billable
)

SELECT
    concat(u.entity_id, u.account, u.cost_centre, u.period, u.scenario) AS pk,
    u.entity_id,
    u.account,
    coalesce(u.hardcoded_account_name, ad.account_name, 'Unknown') AS account_name,
    coalesce(u.hardcoded_account_type, ad.account_type, 'Unknown') AS account_type,
    coalesce(u.hardcoded_fs_line,      ad.fs_line,      'Unknown') AS fs_line,
    u.cost_centre                  AS cost_centre,
    coalesce(cc.department, '')    AS department,
    coalesce(cc.division, '')      AS division,
    u.period                       AS period,
    coalesce(pd.quarter, '')       AS quarter,
    coalesce(pd.fiscal_year, '')   AS fiscal_year,
    u.scenario                     AS scenario,
    u.amount                       AS amount,
    u.rollup_method                AS rollup_method,
    u.commit_id                    AS commit_id
FROM unified u
LEFT JOIN live.dim_account     ad ON u.account     = ad.account_code
LEFT JOIN live.dim_cost_centre cc ON u.cost_centre = cc.cost_centre
LEFT JOIN live.dim_period      pd ON u.period      = pd.period
-- Exclude BS lines from the actuals slice only (plan can include BS).
-- Statistical sections set hardcoded_fs_line='Statistical' so they
-- pass; actuals' fs_line resolves via dim_account.
WHERE NOT (
    u.scenario = 'ACTUALS'
    AND coalesce(u.hardcoded_fs_line, ad.fs_line, '') = 'BS'
)
