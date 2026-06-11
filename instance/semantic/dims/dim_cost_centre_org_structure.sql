-- Generated from dimensions.yml: ragged hierarchy org_structure
-- Flattened hierarchy: all nodes in a single flat table.
-- sort_key ensures natural hierarchy ordering.


WITH divisions AS (
    SELECT DISTINCT
        toString(division)                                 AS node_id,
        division                                           AS node_name,
        concat('[D] ', division)                           AS display_name,
        'division'                                         AS node_type,
        1                                                  AS level,
        NULL                                               AS parent_node_id,
        concat(toString(division), '|')                    AS sort_key
    FROM live.dim_cost_centre
),

departments AS (
    SELECT DISTINCT
        toString(department)                               AS node_id,
        department                                         AS node_name,
        concat('[BU] ', department)                        AS display_name,
        'department'                                       AS node_type,
        2                                                  AS level,
        division                                           AS parent_node_id,
        concat(toString(division), '|', toString(department), '|') AS sort_key
    FROM live.dim_cost_centre
),

cost_centres AS (
    SELECT
        toString(cost_centre)                           AS node_id,
        concat(cost_centre, ' · ', cost_centre_name)    AS node_name,
        concat('[CC] ', concat(cost_centre, ' · ', cost_centre_name)) AS display_name,
        'cost_centre'                                      AS node_type,
        3                                                  AS level,
        department                                         AS parent_node_id,
        concat(toString(division), '|', toString(department), '|', toString(cost_centre)) AS sort_key
    FROM live.dim_cost_centre
),

all_nodes AS (
    -- Synthetic root node — resolves to all, sorts first
    SELECT
        'all'                  AS node_id,
        '— All Cost Centres —' AS node_name,
        '— All Cost Centres —' AS display_name,
        'all'                  AS node_type,
        0                      AS level,
        NULL                   AS parent_node_id,
        ''                     AS sort_key
    UNION ALL
    SELECT node_id, node_name, display_name, node_type, level, parent_node_id, sort_key FROM divisions
    UNION ALL
    SELECT node_id, node_name, display_name, node_type, level, parent_node_id, sort_key FROM departments
    UNION ALL
    SELECT node_id, node_name, display_name, node_type, level, parent_node_id, sort_key FROM cost_centres
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
