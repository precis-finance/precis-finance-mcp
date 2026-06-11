-- Project dimension: master data for project-level analysis.
-- Source: live.dim_project (ingested from projects.projects via
-- the customer_pg__dim_project binding).
--
-- `client_name` is denormalised in here for downstream convenience; resolved
-- via a join to dim_client so dim_project doesn't carry a copy of the
-- client master that can drift from the canonical dim.

SELECT
    p.project_id,
    p.project_code,
    p.project_name,
    p.client_id,
    c.client_name AS client_name,
    p.project_type,
    p.status,
    p.start_date,
    p.end_date,
    p.budget_hours,
    p.budget_revenue   AS budget_revenue_eur,
    p.contract_value   AS contract_value_eur,
    p.project_manager_id,
    p.cost_centre
FROM live.dim_project p
LEFT JOIN live.dim_client c
    ON p.client_id = c.client_id
