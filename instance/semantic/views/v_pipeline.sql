-- Pipeline fact view: opportunity × close-month grain.
-- Source: live.fact_pipeline + dim_crm_account.
--
-- Catalogue domain: `pipeline` (instance/catalogue/pipeline.yml).
-- Engine reads only this view; opportunities and accounts in landing are
-- not exposed directly.
--
-- `weighted_amount` is derived here so metric SQL stays simple — the
-- weighted_pipeline metric is just SUM(weighted_amount).

SELECT
    o.opportunity_id,
    o.account_id        AS crm_account,
    o.account_id        AS account_id,
    a.account_name,
    a.industry,
    a.segment,
    a.region,
    o.stage,
    o.stage_category,
    o.probability,
    o.amount,
    (o.amount * o.probability)                  AS weighted_amount,
    o.currency,
    o.created_date,
    o.close_date,
    formatDateTime(o.close_date, '%Y-%m')       AS period,
    o.last_stage_change_date,
    o.owner,
    o.service_line,
    o.engagement_type,
    o.duration_months,
    o.estimated_start_date,
    o.source,
    'ACTUALS'                                   AS scenario
FROM live.fact_pipeline o
LEFT JOIN live.dim_crm_account a
    ON o.account_id = a.account_id
