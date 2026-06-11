-- CRM account dimension: customer master from the sales-pipeline side.
-- Distinct from `dim_client` (delivery / billed customers) — CRM accounts
-- are sales-pipeline entities and intentionally do not share keys with
-- delivery clients. Agent resolves the cross-domain link at query time.
--
-- Source: live.dim_crm_account (ingested from the
-- crm_filedrop source via the crm_filedrop__dim_crm_account binding).

SELECT
    account_id,
    account_name,
    industry,
    segment,
    region,
    created_date
FROM live.dim_crm_account
