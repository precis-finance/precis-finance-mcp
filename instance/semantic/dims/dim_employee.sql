-- Employee dimension: master data for employee-level analysis.
-- Source: live.dim_employee (ingested from hr.employees via
-- the customer_pg__dim_employee binding).

SELECT
    employee_id,
    employee_code,
    concat(first_name, ' ', last_name) AS employee_name,
    grade,
    employee_type,
    role_title,
    cost_centre,
    daily_bill_rate,
    fte,
    start_date,
    end_date,
    is_active
FROM live.dim_employee
