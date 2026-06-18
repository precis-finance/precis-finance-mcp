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


def build_rollup_sql(ragged_dim: Dimension, catalogue: Catalogue) -> str:
    """Generate the node → leaf mapping SQL (``dim_{leaf}_{key}_rollup``)."""
    leaf_dim = catalogue.dimensions.get(ragged_dim.leaf_dimension)
    if not leaf_dim or not leaf_dim.source:
        raise ValueError(f"Cannot resolve leaf dimension for {ragged_dim.key}")

    source = leaf_dim.source.table
    leaf_col = leaf_dim.source.key_column
    levels = _resolve_level_info(ragged_dim, catalogue)

    lines: list[str] = []
    lines.append(f"-- Generated from dimensions.yml: ragged hierarchy {ragged_dim.key}")
    lines.append(f"-- Node → leaf mapping for hierarchy resolution.")
    lines.append("")

    unions: list[str] = []

    # Non-leaf levels: concat(prefix, column) AS node_id
    for level in levels:
        if level["is_leaf"]:
            continue
        prefix = level["node_prefix"]
        col = level["column"]
        label = level["label"]
        comment = f"-- {label} level: resolves to all {leaf_dim.label}s in that {label.lower()}"
        if prefix:
            select = f"    concat('{prefix}', toString({col})) AS node_id,"
        else:
            select = f"    toString({col}) AS node_id,"
        unions.append(
            f"{comment}\n"
            f"SELECT\n"
            f"{select}\n"
            f"    {leaf_col}\n"
            f"FROM {source}"
        )

    # Leaf level: resolves to itself
    unions.append(
        f"-- {leaf_dim.label} level: resolves to itself\n"
        f"SELECT\n"
        f"    toString({leaf_col}) AS node_id,\n"
        f"    toString({leaf_col}) AS {leaf_col}\n"
        f"FROM {source}"
    )

    # Root: resolves to all
    unions.append(
        f"-- Synthetic root: resolves to all {leaf_dim.label.lower()}s\n"
        f"SELECT\n"
        f"    'all'          AS node_id,\n"
        f"    toString({leaf_col}) AS {leaf_col}\n"
        f"FROM {source}"
    )

    lines.append("\nUNION ALL\n\n".join(unions))
    return "\n".join(lines) + "\n"


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


def _is_generated(dim: Dimension) -> bool:
    """A ragged dimension whose rollup Précis derives (not operator-provided).

    Matches the runtime branch in ``filter_resolver._resolve_ragged_filter``:
    only ``type: provided`` (with a table) reads an external rollup; everything
    else is generated here.
    """
    if not dim.is_ragged or not dim.leaf_dimension:
        return False
    rs = dim.ragged_source
    if rs and rs.type == "provided" and rs.table:
        return False
    return True


def build_ragged_views(catalogue: Catalogue) -> list[tuple[str, str]]:
    """Return ``(view_name, sql_body)`` for every generated ragged dimension.

    Two entries per dimension: the ``*_rollup`` mapping view and the flattened
    node-list view. ``sql_body`` is bare SQL (no ``CREATE OR REPLACE VIEW``
    wrapper — the caller wraps). Provided ragged dimensions are skipped.
    """
    views: list[tuple[str, str]] = []
    for dim in catalogue.dimensions.values():
        if not _is_generated(dim):
            continue
        leaf = dim.leaf_dimension
        views.append((f"dim_{leaf}_{dim.key}_rollup", build_rollup_sql(dim, catalogue)))
        views.append((f"dim_{leaf}_{dim.key}", build_hierarchy_sql(dim, catalogue)))
    return views
