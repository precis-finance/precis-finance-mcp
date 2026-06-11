-- Account dimension: full Chart of Accounts from the ERP master
-- Excludes HEADER rows (rollup parents, never postable)

SELECT
    account_code,
    account_name,
    account_type,
    fs_line
FROM live.dim_account
WHERE is_active = TRUE
  AND account_type != 'HEADER'
ORDER BY account_code
