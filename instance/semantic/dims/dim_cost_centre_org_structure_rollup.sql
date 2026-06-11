-- Generated from dimensions.yml: ragged hierarchy org_structure
-- Node → leaf mapping for hierarchy resolution.

-- Division level: resolves to all Cost Centres in that division
SELECT
    toString(division) AS node_id,
    cost_centre
FROM live.dim_cost_centre
UNION ALL

-- Department level: resolves to all Cost Centres in that department
SELECT
    toString(department) AS node_id,
    cost_centre
FROM live.dim_cost_centre
UNION ALL

-- Cost Centre level: resolves to itself
SELECT
    toString(cost_centre) AS node_id,
    toString(cost_centre) AS cost_centre
FROM live.dim_cost_centre
UNION ALL

-- Synthetic root: resolves to all cost centres
SELECT
    'all'          AS node_id,
    toString(cost_centre) AS cost_centre
FROM live.dim_cost_centre
