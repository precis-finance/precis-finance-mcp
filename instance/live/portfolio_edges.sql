-- Declared shape of the solution_portfolio child→parent edges.
--
-- Snapshot binding — no PARTITION BY; swap is EXCHANGE TABLES.
--
-- Raw projection of `master.portfolio_edges` on the customer's Postgres. One
-- row per child→parent roll-up; a child may appear under several parents.
-- This edge set is the sole source of hierarchy topology; the recursive
-- `_rollup` (node → all its leaves) is derived from it downstream.

(
    child_node_id  String,
    parent_node_id String,
    _load_id       String,
    _ingested_at   DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (child_node_id, parent_node_id)
