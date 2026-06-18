-- Declared shape of the intercompany recharge fact (intra-group cross-charges).
-- A cost centre (`cost_centre`) is recharged by a counterparty cost centre
-- (`counterparty_cc`) in `period`. Not joined to the GL.
--
-- Applied to both live.* and staging.* with identical engine spec; PARTITION BY
-- period gives per-period atomic REPLACE on the swap.

(
    period           String,
    cost_centre      String,
    counterparty_cc  String,
    amount           Decimal(18, 2),
    _load_id         String,
    _ingested_at     DateTime DEFAULT now()
)
ENGINE = MergeTree
PARTITION BY period
ORDER BY (period, cost_centre, counterparty_cc)
