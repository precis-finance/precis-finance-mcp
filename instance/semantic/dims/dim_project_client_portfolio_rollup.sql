-- Generated from dimensions.yml: ragged hierarchy client_portfolio
-- Node → leaf mapping for hierarchy resolution.

-- Client level: resolves to all Projects in that client
SELECT
    concat('CLI::', toString(client_id)) AS node_id,
    toString(project_id)                 AS project_id
FROM live.dim_project
UNION ALL

-- Project level: resolves to itself
SELECT
    toString(project_id) AS node_id,
    toString(project_id) AS project_id
FROM live.dim_project
UNION ALL

-- Synthetic root: resolves to all projects
SELECT
    'all'                AS node_id,
    toString(project_id) AS project_id
FROM live.dim_project
