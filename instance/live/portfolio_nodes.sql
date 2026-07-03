-- Declared shape of the solution_portfolio node master.
--
-- Snapshot binding — no PARTITION BY; swap is EXCHANGE TABLES.
--
-- Raw projection of `master.portfolio_nodes` on the customer's Postgres — the
-- non-leaf (portfolio/solution) nodes only. Leaf cost-centre nodes are
-- auto-injected into the semantic node view, not stored here.

(
    node_id      String,
    node_name    String,
    node_type    String,
    _load_id     String,
    _ingested_at DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY node_id
