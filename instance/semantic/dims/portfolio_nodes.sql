-- Pass-through of the operator-provided solution_portfolio node master.
-- The catalogue reads only the semantic layer; the ragged-view generator
-- reads semantic.portfolio_nodes to build the hierarchy's node master.
SELECT * FROM live.portfolio_nodes
