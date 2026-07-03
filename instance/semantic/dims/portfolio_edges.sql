-- Pass-through of the operator-provided solution_portfolio child→parent edges.
-- The ragged-view generator reads semantic.portfolio_edges to build the
-- normalised edges view and the recursive rollup.
SELECT * FROM live.portfolio_edges
