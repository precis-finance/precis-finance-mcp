-- Declared shape of the employee dimension.
--
-- Snapshot binding — no PARTITION BY; swap is EXCHANGE TABLES.
-- Raw projection of `hr.employees` on the customer's Postgres.
-- Source column `cost_centre_id` is renamed to `cost_centre` at the
-- binding boundary.

(
    employee_id        Int32,
    employee_code      String,
    first_name         Nullable(String),
    last_name          Nullable(String),
    email              Nullable(String),
    grade              Nullable(String),
    employee_type      Nullable(String),
    role_title         Nullable(String),
    cost_centre        Nullable(String),
    annual_cost        Nullable(Decimal(12, 2)),
    daily_bill_rate    Nullable(Decimal(10, 2)),
    fte                Nullable(Decimal(3, 2)),
    start_date         Nullable(Date),
    end_date           Nullable(Date),
    is_active          Nullable(UInt8),
    currency           Nullable(String),
    _load_id           String,
    _ingested_at       DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY employee_id
