-- Generated from dimensions.yml: ragged hierarchy calendar
-- Flattened hierarchy: all nodes in a single flat table.
-- sort_key ensures natural hierarchy ordering.


WITH fiscal_years AS (
    SELECT DISTINCT
        toString(fiscal_year)                              AS node_id,
        fiscal_year                                        AS node_name,
        fiscal_year                                        AS display_name,
        'fiscal_year'                                      AS node_type,
        1                                                  AS level,
        NULL                                               AS parent_node_id,
        concat(toString(fiscal_year), '|')                 AS sort_key
    FROM live.dim_period
),

quarters AS (
    SELECT DISTINCT
        toString(quarter)                                  AS node_id,
        quarter                                            AS node_name,
        quarter                                            AS display_name,
        'quarter'                                          AS node_type,
        2                                                  AS level,
        fiscal_year                                        AS parent_node_id,
        concat(toString(fiscal_year), '|', toString(quarter), '|') AS sort_key
    FROM live.dim_period
),

periods AS (
    SELECT
        toString(period)                                   AS node_id,
        period                                             AS node_name,
        period                                             AS display_name,
        'period'                                           AS node_type,
        3                                                  AS level,
        quarter                                            AS parent_node_id,
        concat(toString(fiscal_year), '|', toString(quarter), '|', toString(period)) AS sort_key
    FROM live.dim_period
),

all_nodes AS (
    -- Synthetic root node — resolves to all, sorts first
    SELECT
        'all'                  AS node_id,
        '— All Periods —' AS node_name,
        '— All Periods —' AS display_name,
        'all'                  AS node_type,
        0                      AS level,
        NULL                   AS parent_node_id,
        ''                     AS sort_key
    UNION ALL
    SELECT node_id, node_name, display_name, node_type, level, parent_node_id, sort_key FROM fiscal_years
    UNION ALL
    SELECT node_id, node_name, display_name, node_type, level, parent_node_id, sort_key FROM quarters
    UNION ALL
    SELECT node_id, node_name, display_name, node_type, level, parent_node_id, sort_key FROM periods
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
