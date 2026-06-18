-- Declared shape of the payroll fact.
--
-- Source: `hr.payroll` on customer Postgres. One row per
-- (employee, pay_date); pay_date is monthly. Identity projection at the
-- binding boundary — no aggregation.

(
    period                   String,
    pay_date                 Date,
    employee_id              Int32,
    cost_centre              String,
    gross_salary             Decimal(14, 2),
    employer_contributions   Decimal(14, 2),
    bonus                    Decimal(14, 2),
    total_cost               Decimal(14, 2),
    currency                 String,
    _load_id                 String,
    _ingested_at             DateTime DEFAULT now()
)
ENGINE = MergeTree
PARTITION BY period
ORDER BY (period, employee_id)
