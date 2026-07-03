# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Derive ragged-hierarchy rollup views from the catalogue.

A `type: generated` ragged dimension declares its hierarchy as ordered
`levels` in `instance/catalogue/dimensions.yml`. The runtime needs two
ClickHouse views per such dimension:

  - ``dim_{leaf}_{key}_rollup`` — node_id → leaf-key mapping; the filter
    resolver and ``search_hierarchy`` query it (``filter_resolver.py``,
    ``read_tools.py``).
  - ``dim_{leaf}_{key}`` — the flattened node list (node_id, names, level,
    parent_node_id, sort_key) for display / hierarchy navigation.

These views are pure derivations of the catalogue, so they are generated
here and applied to ClickHouse by ``semantic_runner.apply_all`` — they are
never written to ``instance/``, which holds only operator-authored config.
A ``type: provided`` ragged dimension supplies its own rollup table and is
skipped here.

These views are generated in memory and emitted as plain ClickHouse SQL.
"""

from __future__ import annotations

from precis_mcp.engine.catalogue import Catalogue, Dimension


def _resolve_level_info(ragged_dim: Dimension, catalogue: Catalogue) -> list[dict]:
    """Build per-level info ordered root → leaf, resolving dimension references.

    Each dict carries the source column for the level, prefixes, the leaf
    display attribute, and — for a non-leaf level backed by its own source
    table — the JOIN needed to resolve that level's display name.
    """
    dims = catalogue.dimensions
    leaf_dim = dims.get(ragged_dim.leaf_dimension)
    levels: list[dict] = []

    for i, rl in enumerate(ragged_dim.ragged_levels):
        is_leaf = (i == len(ragged_dim.ragged_levels) - 1)
        dim = dims.get(rl.dimension)
        if not dim:
            continue

        join_source = ""
        join_key = ""
        join_display_col = ""

        if dim.is_derived and dim.derived_from:
            column = dim.derived_from.source_column
        elif dim.is_leaf and dim.source:
            if is_leaf:
                # Leaf level of the hierarchy — use the key column directly
                column = dim.source.key_column
            elif leaf_dim and rl.dimension in leaf_dim.parents:
                # Non-leaf level backed by its own source table — use the FK
                # column from the hierarchy's leaf dimension, JOIN for display.
                column = leaf_dim.parents[rl.dimension].source_column
                join_source = dim.source.table
                join_key = dim.source.key_column
                if dim.display_attribute:
                    join_display_col = dim.source.attribute_mapping.get(
                        dim.display_attribute, ""
                    )
            else:
                column = dim.source.key_column
        else:
            continue

        node_prefix = rl.node_prefix if not is_leaf else ""

        display_attr_col = ""
        if is_leaf and dim.display_attribute and dim.source:
            display_attr_col = dim.source.attribute_mapping.get(dim.display_attribute, "")

        levels.append({
            "dimension_key": rl.dimension,
            "column": column,
            "display_prefix": rl.display_prefix,
            "node_prefix": node_prefix,
            "label": dim.label,
            "is_leaf": is_leaf,
            "display_attribute_col": display_attr_col,
            "join_source": join_source,
            "join_key": join_key,
            "join_display_col": join_display_col,
        })

    return levels


def build_generated_edges_sql(ragged_dim: Dimension, catalogue: Catalogue) -> str:
    """Derive child→parent edges from the leaf table's ancestor columns.

    The generated counterpart to a provided edge table: for each adjacent pair
    of declared levels it emits the deeper→shallower node relationship, plus an
    edge from the top level to the synthetic ``all`` root, so the recursive
    rollup reproduces the full closure (including ``all`` → every leaf). Node-id
    expressions match ``build_hierarchy_sql`` (same prefix/column logic) so the
    edges, node master, and rollup all agree on node ids.
    """
    leaf_dim = catalogue.dimensions.get(ragged_dim.leaf_dimension)
    if not leaf_dim or not leaf_dim.source:
        raise ValueError(f"Cannot resolve leaf dimension for {ragged_dim.key}")
    source = leaf_dim.source.table
    levels = _resolve_level_info(ragged_dim, catalogue)

    def node_id(level: dict) -> str:
        prefix = level["node_prefix"]
        col = level["column"]
        return f"concat('{prefix}', toString({col}))" if prefix else f"toString({col})"

    unions: list[str] = []
    # Deeper → shallower for each adjacent level pair (leaf-most first).
    for i in range(len(levels) - 1, 0, -1):
        unions.append(
            f"-- {levels[i]['label']} → {levels[i - 1]['label']}\n"
            f"SELECT DISTINCT\n"
            f"    {node_id(levels[i])} AS child_node_id,\n"
            f"    {node_id(levels[i - 1])} AS parent_node_id\n"
            f"FROM {source}"
        )
    # Top level → synthetic 'all' root.
    unions.append(
        f"-- {levels[0]['label']} → root\n"
        f"SELECT DISTINCT\n"
        f"    {node_id(levels[0])} AS child_node_id,\n"
        f"    'all' AS parent_node_id\n"
        f"FROM {source}"
    )

    header = (
        f"-- Generated from dimensions.yml: edges for ragged hierarchy {ragged_dim.key}\n\n"
    )
    return header + "\n\nUNION ALL\n\n".join(unions) + "\n"


def build_rollup_sql(ragged_dim: Dimension, catalogue: Catalogue) -> str:
    """Recursive node → leaf rollup over the hierarchy's ``*_edges`` view.

    Shared by generated and provided hierarchies — both read the fixed-name
    ``semantic.dim_{leaf}_{key}_edges`` view produced alongside. The recursive
    closure replaces the former per-level UNION-ALL derivation so a single code
    path serves both, and multi-parent / arbitrary-depth edges resolve.
    """
    leaf_dim = catalogue.dimensions.get(ragged_dim.leaf_dimension)
    if not leaf_dim or not leaf_dim.source:
        raise ValueError(f"Cannot resolve leaf dimension for {ragged_dim.key}")
    stem = f"dim_{ragged_dim.leaf_dimension}_{ragged_dim.key}"
    return build_recursive_rollup_sql(
        leaf_source=leaf_dim.source.table,
        leaf_key=leaf_dim.source.key_column,
        edges_view=f"semantic.{stem}_edges",
    )


def build_hierarchy_sql(ragged_dim: Dimension, catalogue: Catalogue) -> str:
    """Generate the flattened hierarchy view SQL (``dim_{leaf}_{key}``)."""
    leaf_dim = catalogue.dimensions.get(ragged_dim.leaf_dimension)
    if not leaf_dim or not leaf_dim.source:
        raise ValueError(f"Cannot resolve leaf dimension for {ragged_dim.key}")

    source = leaf_dim.source.table
    levels = _resolve_level_info(ragged_dim, catalogue)

    lines: list[str] = []
    lines.append(f"-- Generated from dimensions.yml: ragged hierarchy {ragged_dim.key}")
    lines.append(f"-- Flattened hierarchy: all nodes in a single flat table.")
    lines.append(f"-- sort_key ensures natural hierarchy ordering.")
    lines.append("")

    cte_names: list[str] = []

    # Generate a CTE for each level
    for level_num, level in enumerate(levels):
        is_leaf = level["is_leaf"]
        cte_name = level["label"].lower().replace(" ", "_") + "s"
        cte_names.append(cte_name)

        display_prefix = level["display_prefix"]
        col = level["column"]
        prefix = level["node_prefix"]

        # When a non-leaf level is backed by its own source table, we JOIN
        # that table to resolve display names.  Qualify columns with _m / _d.
        has_join = bool(level.get("join_source"))
        if has_join:
            join_tbl = level["join_source"]
            from_clause = (
                f"{source} _m\n"
                f"    JOIN {join_tbl} _d\n"
                f"        ON _m.{col} = _d.{level['join_key']}"
            )
            col_ref = f"_m.{col}"
            display_col = f"_d.{level['join_display_col']}" if level["join_display_col"] else col_ref
        else:
            from_clause = source
            col_ref = col
            display_col = col

        # node_id
        if prefix:
            node_id_expr = f"concat('{prefix}', toString({col_ref}))"
        else:
            node_id_expr = f"toString({col_ref})"

        # node_name — use display column (joined name) when available
        if is_leaf and level["display_attribute_col"]:
            attr_col = level["display_attribute_col"]
            node_name_expr = f"concat({col_ref}, ' · ', {attr_col})"
        elif has_join and display_col != col_ref:
            node_name_expr = display_col
        else:
            node_name_expr = col_ref

        # display_name
        if display_prefix:
            display_name_expr = f"concat('{display_prefix}', {node_name_expr})"
        else:
            display_name_expr = node_name_expr

        # parent_node_id
        if level_num == 0:
            parent_expr = "NULL"
        else:
            parent_level = levels[level_num - 1]
            parent_col = parent_level["column"]
            parent_col_ref = f"_m.{parent_col}" if has_join else parent_col
            if parent_level["node_prefix"]:
                parent_expr = f"concat('{parent_level['node_prefix']}', toString({parent_col_ref}))"
            else:
                parent_expr = parent_col_ref

        # sort_key — use display column for joined levels (readable ordering)
        sort_parts = []
        for i in range(level_num + 1):
            lv = levels[i]
            lv_col = lv["column"]
            if has_join and lv.get("join_display_col"):
                if i == level_num:
                    sort_parts.append(f"toString({display_col})")
                else:
                    sort_parts.append(f"toString({('_m.' + lv_col) if has_join else lv_col})")
            else:
                sort_parts.append(f"toString({('_m.' + lv_col) if has_join else lv_col})")
        concat_args = ", '|', ".join(sort_parts)
        sort_key_expr = f"concat({concat_args}, '|')" if not is_leaf else f"concat({concat_args})"

        sql_level = level_num + 1
        node_type = level["dimension_key"]

        distinct = "DISTINCT" if not is_leaf else ""
        select_kw = f"SELECT {distinct}".rstrip()

        cte_sql = (
            f"{cte_name} AS (\n"
            f"    {select_kw}\n"
            f"        {node_id_expr:50s} AS node_id,\n"
            f"        {node_name_expr:50s} AS node_name,\n"
            f"        {display_name_expr:50s} AS display_name,\n"
            f"        '{node_type}'{' ' * (48 - len(node_type))} AS node_type,\n"
            f"        {sql_level}{' ' * 49} AS level,\n"
            f"        {parent_expr:50s} AS parent_node_id,\n"
            f"        {sort_key_expr:50s} AS sort_key\n"
            f"    FROM {from_clause}\n"
            f")"
        )
        lines.append(cte_sql)

    # Build WITH + all_nodes union
    with_block = "WITH " + ",\n\n".join(lines[4:])  # skip the comment header
    header = "\n".join(lines[:4])

    # all_nodes CTE
    root_label = ragged_dim.root_label or f"— All {leaf_dim.label}s —"
    all_nodes_parts = [
        f"all_nodes AS (\n"
        f"    -- Synthetic root node — resolves to all, sorts first\n"
        f"    SELECT\n"
        f"        'all'                  AS node_id,\n"
        f"        '{root_label}' AS node_name,\n"
        f"        '{root_label}' AS display_name,\n"
        f"        'all'                  AS node_type,\n"
        f"        0                      AS level,\n"
        f"        NULL                   AS parent_node_id,\n"
        f"        ''                     AS sort_key"
    ]
    for cn in cte_names:
        all_nodes_parts.append(
            f"    UNION ALL\n"
            f"    SELECT node_id, node_name, display_name, node_type, level, "
            f"parent_node_id, sort_key FROM {cn}"
        )
    all_nodes_parts.append(")")

    # Final SELECT
    final_select = (
        "SELECT\n"
        "    node_id,\n"
        "    display_name,\n"
        "    node_name,\n"
        "    node_type,\n"
        "    level,\n"
        "    parent_node_id,\n"
        "    sort_key\n"
        "FROM all_nodes\n"
        "ORDER BY sort_key, level"
    )

    result = (
        header + "\n\n"
        + with_block + ",\n\n"
        + "\n".join(all_nodes_parts) + "\n\n"
        + final_select + "\n"
    )
    return result


def build_recursive_rollup_sql(*, leaf_source: str, leaf_key: str, edges_view: str) -> str:
    """Generate the node → leaf rollup as a recursive walk over an edge view.

    The TO-BE rollup shared by the provided and (post-homogenisation) generated
    paths. Given a child→parent ``edges_view`` (columns ``child_node_id`` /
    ``parent_node_id``) and the leaf dimension's ``leaf_source`` / ``leaf_key``,
    it emits the closure ``(node_id, {leaf_key})`` — every node mapped to all
    the leaves beneath it.

    Each leaf seeds the walk mapped to itself; the recursive term climbs one
    edge at a time, carrying the visited-node ``_path`` so a cyclic edge set
    cannot loop forever. ``SELECT DISTINCT`` collapses a leaf reachable by more
    than one path (a diamond) to a single ``(node_id, leaf)`` row — the reason
    the single-parent UNION-ALL derivation is not a drop-in port.
    """
    return _recursive_rollup_cte(
        leaf_source=leaf_source, leaf_key=leaf_key, edges_view=edges_view
    ) + (
        "SELECT DISTINCT\n"
        "    node_id,\n"
        f"    {leaf_key}\n"
        "FROM rollup\n"
    )


def _recursive_rollup_cte(*, leaf_source: str, leaf_key: str, edges_view: str) -> str:
    """The ``WITH RECURSIVE rollup AS (...)`` block shared by the rollup view and
    the diamond check. One row per ``(node_id, leaf, path)`` — the caller either
    dedups (the rollup) or counts paths (the diamond check)."""
    return (
        "WITH RECURSIVE rollup AS (\n"
        "    -- Base: every leaf resolves to itself\n"
        "    SELECT\n"
        f"        toString({leaf_key}) AS node_id,\n"
        f"        toString({leaf_key}) AS {leaf_key},\n"
        f"        [toString({leaf_key})] AS _path\n"
        f"    FROM {leaf_source}\n"
        "    UNION ALL\n"
        "    -- Step: climb one child→parent edge, guarding against cycles\n"
        "    SELECT\n"
        "        e.parent_node_id AS node_id,\n"
        f"        r.{leaf_key} AS {leaf_key},\n"
        "        arrayPushBack(r._path, e.parent_node_id) AS _path\n"
        "    FROM rollup AS r\n"
        f"    INNER JOIN {edges_view} AS e ON e.child_node_id = r.node_id\n"
        "    WHERE NOT has(r._path, e.parent_node_id)\n"
        ")\n"
    )


def build_diamond_check_sql(
    *, leaf_source: str, leaf_key: str, edges_view: str, limit: int = 50
) -> str:
    """Rows where a leaf reaches a node by **more than one path** (a diamond).

    Same recursive walk as the rollup but grouped without the final ``DISTINCT``:
    a ``(node_id, leaf)`` pair with ``paths > 1`` is a reconvergence. Returns
    ``(node_id, {leaf_key}, paths)``; an empty result means no diamonds. Used for
    an operator-log warning — the rollup itself dedups, so this never blocks.
    """
    return _recursive_rollup_cte(
        leaf_source=leaf_source, leaf_key=leaf_key, edges_view=edges_view
    ) + (
        "SELECT\n"
        "    node_id,\n"
        f"    {leaf_key},\n"
        "    count() AS paths\n"
        "FROM rollup\n"
        f"GROUP BY node_id, {leaf_key}\n"
        "HAVING count() > 1\n"
        "ORDER BY paths DESC, node_id\n"
        f"LIMIT {int(limit)}\n"
    )


def build_orphan_edges_sql(*, node_master: str, edges_view: str, limit: int = 50) -> str:
    """Edge endpoints absent from the node master — orphan edges.

    The node master carries every valid node (operator/level nodes, the injected
    leaves, and — generated only — the ``all`` root), so any edge whose child or
    parent id is not present references a node that does not exist. Returns
    ``(node_id, role)``; an empty result means no orphans. Used for an
    operator-log warning — the recursive rollup ignores such edges anyway.
    """
    return (
        "SELECT node_id, role FROM (\n"
        f"    SELECT DISTINCT child_node_id AS node_id, 'child' AS role FROM {edges_view}\n"
        "    UNION ALL\n"
        f"    SELECT DISTINCT parent_node_id AS node_id, 'parent' AS role FROM {edges_view}\n"
        ")\n"
        f"WHERE node_id NOT IN (SELECT node_id FROM {node_master})\n"
        f"LIMIT {int(limit)}\n"
    )


def build_provided_edges_sql(ragged_dim: Dimension, catalogue: Catalogue) -> str:
    """Normalise a provided edge table to ``(child_node_id, parent_node_id)``.

    The operator's edge table may name its columns anything; this view renames
    them to the platform's column contract so the recursive rollup is agnostic
    to the source shape.
    """
    rs = ragged_dim.ragged_source
    if rs is None:
        raise ValueError(f"Provided ragged dimension {ragged_dim.key} has no ragged_source")
    return (
        "-- Provided child→parent edges normalised to the platform columns.\n"
        "SELECT\n"
        f"    {rs.child_column} AS child_node_id,\n"
        f"    {rs.parent_column} AS parent_node_id\n"
        f"FROM {rs.edge_table}"
    )


def build_provided_node_master_sql(ragged_dim: Dimension, catalogue: Catalogue) -> str:
    """Node master for a provided hierarchy: operator nodes + injected leaves.

    The operator supplies only the non-leaf nodes; every leaf is auto-injected
    from the leaf dimension so the node master is self-contained (matching the
    generated path). ``display_name`` is derived as a copy of ``node_name``.
    """
    rs = ragged_dim.ragged_source
    if rs is None:
        raise ValueError(f"Provided ragged dimension {ragged_dim.key} has no ragged_source")
    leaf_dim = catalogue.dimensions.get(ragged_dim.leaf_dimension)
    if not leaf_dim or not leaf_dim.source:
        raise ValueError(f"Cannot resolve leaf dimension for {ragged_dim.key}")
    leaf_src = leaf_dim.source.table
    leaf_key = leaf_dim.source.key_column
    leaf_name = f"toString({leaf_key})"
    if leaf_dim.display_attribute:
        disp = leaf_dim.source.attribute_mapping.get(leaf_dim.display_attribute, "")
        if disp:
            leaf_name = disp
    return (
        "-- Operator-provided nodes; display_name derived as node_name.\n"
        "SELECT\n"
        "    node_id,\n"
        "    node_name,\n"
        "    node_name AS display_name,\n"
        "    node_type\n"
        f"FROM {rs.node_table}\n"
        "UNION ALL\n"
        f"-- Auto-injected leaf nodes ({ragged_dim.leaf_dimension}).\n"
        "SELECT\n"
        f"    toString({leaf_key}) AS node_id,\n"
        f"    {leaf_name} AS node_name,\n"
        f"    {leaf_name} AS display_name,\n"
        f"    '{ragged_dim.leaf_dimension}' AS node_type\n"
        f"FROM {leaf_src}"
    )


def build_ragged_views(catalogue: Catalogue) -> list[tuple[str, str]]:
    """Return ``(view_name, sql_body)`` for every ragged dimension.

    ``sql_body`` is bare SQL (no ``CREATE OR REPLACE VIEW`` wrapper — the caller
    wraps). Both paths emit three views — the ``*_edges`` relationships, the
    node master, and the recursive ``*_rollup`` — and share one rollup code
    path (``build_rollup_sql``). Only how the edges and node master are derived
    differs:

    - **generated**: edges from the leaf table's ancestor columns; node master
      is the flattened, prefixed hierarchy list (with the ``all`` root).
    - **provided**: edges normalised from the operator edge table; node master
      is the operator nodes plus auto-injected leaves.

    Edges are emitted before the rollup that reads them (CH validates refs at
    CREATE VIEW time).
    """
    views: list[tuple[str, str]] = []
    for dim in catalogue.dimensions.values():
        if not dim.is_ragged or not dim.leaf_dimension:
            continue
        stem = f"dim_{dim.leaf_dimension}_{dim.key}"
        rs = dim.ragged_source
        if rs and rs.type == "provided":
            views.append((f"{stem}_edges", build_provided_edges_sql(dim, catalogue)))
            views.append((stem, build_provided_node_master_sql(dim, catalogue)))
        else:
            views.append((f"{stem}_edges", build_generated_edges_sql(dim, catalogue)))
            views.append((stem, build_hierarchy_sql(dim, catalogue)))
        views.append((f"{stem}_rollup", build_rollup_sql(dim, catalogue)))
    return views
