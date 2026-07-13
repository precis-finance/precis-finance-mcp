-- Declared shape of the ISO-week calendar dimension.
--
-- Snapshot binding — no PARTITION BY; swap is EXCHANGE TABLES, which
-- swaps the full table atomically. ORDER BY is required by MergeTree.
--
-- `week` is the canonical ISO week code (YYYY-Www, zero-padded so it sorts
-- chronologically). `seq` is a dense global ordinal over ordered weeks — the
-- authority for prior-period at week grain (predecessor = seq - 1), which has
-- no closed form across the 52/53-week year boundary. Populated at ingestion
-- via row_number() OVER (ORDER BY week) in the binding extract.

(
    week          String,
    seq           UInt32,
    _load_id      String,
    _ingested_at  DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY week
