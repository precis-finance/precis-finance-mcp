-- Declared shape of the day (business-day) calendar dimension.
--
-- Snapshot binding — no PARTITION BY; swap is EXCHANGE TABLES, which
-- swaps the full table atomically. ORDER BY is required by MergeTree.
--
-- `day` is the canonical date code (YYYY-MM-DD, which sorts chronologically).
-- `seq` is a dense global ordinal over ordered days — the authority for
-- prior-period at day grain (predecessor = seq - 1). The calendar holds
-- business days only, so the predecessor of a Monday is the previous working
-- day, not the weekend. Populated at ingestion via row_number() OVER (ORDER BY
-- day) in the binding extract.

(
    day           String,
    seq           UInt32,
    _load_id      String,
    _ingested_at  DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY day
