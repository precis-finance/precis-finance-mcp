-- Generated from dimensions.yml: ragged hierarchy calendar
-- Node → leaf mapping for hierarchy resolution.

-- Fiscal Year level: resolves to all Periods in that fiscal year
SELECT
    toString(fiscal_year) AS node_id,
    period
FROM live.dim_period
UNION ALL

-- Quarter level: resolves to all Periods in that quarter
SELECT
    toString(quarter) AS node_id,
    period
FROM live.dim_period
UNION ALL

-- Period level: resolves to itself
SELECT
    toString(period) AS node_id,
    toString(period) AS period
FROM live.dim_period
UNION ALL

-- Synthetic root: resolves to all periods
SELECT
    'all'          AS node_id,
    toString(period) AS period
FROM live.dim_period
