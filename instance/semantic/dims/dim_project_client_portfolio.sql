-- Generated from dimensions.yml: ragged hierarchy client_portfolio
-- Flattened hierarchy: all nodes in a single flat table.
-- sort_key ensures natural hierarchy ordering.


WITH clients AS (
    SELECT DISTINCT
        concat('CLI::', toString(_m.client_id))            AS node_id,
        _d.client_name                                     AS node_name,
        concat('[Client] ', _d.client_name)                AS display_name,
        'client'                                           AS node_type,
        1                                                  AS level,
        NULL                                               AS parent_node_id,
        concat(toString(_d.client_name), '|')              AS sort_key
    FROM live.dim_project _m
    JOIN live.dim_client _d
        ON _m.client_id = _d.client_id
),

projects AS (
    SELECT
        toString(project_id)                               AS node_id,
        concat(toString(project_id), ' · ', project_name)  AS node_name,
        concat('[Project] ', toString(project_id), ' · ', project_name) AS display_name,
        'project'                                          AS node_type,
        2                                                  AS level,
        concat('CLI::', toString(client_id))               AS parent_node_id,
        concat(toString(client_id), '|', toString(project_id)) AS sort_key
    FROM live.dim_project
),

all_nodes AS (
    -- Synthetic root node — resolves to all, sorts first
    SELECT
        'all'                  AS node_id,
        '— All Clients —' AS node_name,
        '— All Clients —' AS display_name,
        'all'                  AS node_type,
        0                      AS level,
        NULL                   AS parent_node_id,
        ''                     AS sort_key
    UNION ALL
    SELECT node_id, node_name, display_name, node_type, level, parent_node_id, sort_key FROM clients
    UNION ALL
    SELECT node_id, node_name, display_name, node_type, level, parent_node_id, sort_key FROM projects
)

SELECT
    node_id,
    display_name,
    node_name,
    node_type,
    level,
    parent_node_id,
    sort_key
FROM all_nodes
ORDER BY sort_key, level
