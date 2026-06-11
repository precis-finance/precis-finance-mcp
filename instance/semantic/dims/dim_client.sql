-- Client dimension: master data for client-level analysis.
-- Source: live.dim_client (ingested from projects.clients via
-- the customer_pg__dim_client binding).

SELECT
    client_id,
    client_name,
    industry,
    tier,
    country
FROM live.dim_client
