# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Shared bounded chart computation; persistence belongs to commercial workflows."""
from __future__ import annotations
import json
import uuid
from precis_mcp.datasets import fetch_cached_read_result, normalize_data_refs
from precis_mcp.js_eval import run_js_transform, validate_transform_code
from precis_mcp.chart_builder import consolidate_captions

CHART_TYPES = ("line", "bar", "area", "waterfall", "scatter", "horizontal_bar", "forecast_line", "cohort_heatmap", "sparkline_row")


def eval_chart_transform(data_refs: list[str], transform_code: str, chart_spec: dict, user_id: str) -> dict:
    """Transform bounded snapshots into a chart. Pass data_refs as an array.

    Query first, inspect the returned rows, then supply a JavaScript function
    taking one engine result per reference, in order, and returning flat rows.
    Use detail grain for ordinary series, row.item for metric identity,
    row.dimensions for dimensions and row.values for scenario values.
    Cached values are already scaled: scale 3 divides non-exempt metrics by
    1000; scale 0 leaves them in units. Percentages (75 means 75%), FTEs and
    other scale-exempt metrics retain their units. Do not scale values again.
    For multiple inputs, inspect each scale and item format; re-query same-unit
    series at a common scale before combining them on one axis. Use separately
    labelled axes for currency/FTE or currency/percent (line/bar/area only),
    or separate charts. Conflicting nonempty caption scale labels are omitted;
    unlabelled sources do not remove other labels. Axis labels must state each
    unit. Values are not auto-converted.
    chart_spec requires chart_type, x_key and y_keys; engine precision drives
    left-axis decimals. This computes a chart; it performs no report writes.
    """
    refs = normalize_data_refs(data_refs)
    if not refs:
        return {"status": "error", "error": "data_refs must be an array of valid dataset references"}
    if not transform_code or not transform_code.strip():
        return {"status": "error", "error": "No transform code provided"}
    # Validate the transform structurally at author time (data-independent),
    # so a malformed transform is rejected here rather than silently failing
    # on every later report refresh.
    tx_err = validate_transform_code(transform_code)
    if tx_err:
        return {"status": "error", "error": tx_err}
    # LLMs sometimes double-encode dict params as JSON strings
    if isinstance(chart_spec, str):
        try:
            chart_spec = json.loads(chart_spec)
        except (json.JSONDecodeError, TypeError):
            pass
    if not chart_spec or not isinstance(chart_spec, dict):
        return {"status": "error", "error": "chart_spec must be a dict"}

    # Validate chart_spec
    chart_type = chart_spec.get("chart_type")
    if chart_type not in CHART_TYPES:
        return {
            "status": "error",
            "error": f"Invalid chart_type '{chart_type}'. Use one of: "
                     f"line, bar, area, waterfall, scatter, horizontal_bar, "
                     f"forecast_line, cohort_heatmap, sparkline_row. "
                     f"(A KPI headline strip is not a chart — present it as "
                     f"a financial_table or text.)",
        }
    if not isinstance(chart_spec.get("x_key"), str) or not chart_spec["x_key"]:
        return {"status": "error", "error": "chart_spec.x_key is required"}
    if not isinstance(chart_spec.get("y_keys"), list) or not chart_spec["y_keys"] or not all(isinstance(key, str) and key for key in chart_spec["y_keys"]):
        return {"status": "error", "error": "chart_spec.y_keys is required"}

    engine_results: list[dict] = []
    sources: list[dict] = []
    for ref in refs:
        payload, err = fetch_cached_read_result(user_id, ref)
        if err:
            return {"status": "error", "error": err}
        engine_results.append(payload.get("result", {}))
        sources.append(payload.get("source", {}))

    engine_jsons = [json.dumps(er) for er in engine_results]

    # The first result drives left-axis scale/decimals — the agent cannot
    # override. Data is already scaled by the engine, so y_scale is 0.
    # (Right-axis decimals come from chart_spec.y_decimals_right.)
    engine_decimals = engine_results[0].get("decimals", 1)

    # source: a bare dict for the single-input case (unchanged on-disk
    # shape for existing charts), a list when combining multiple refs.
    source = sources[0] if len(sources) == 1 else sources

    # Caption: consolidate the per-source captions into one display caption.
    engine_caption = consolidate_captions(
        [er.get("caption") for er in engine_results]
    )

    # Run JS transform (variadic — one arg per input, in ref order)
    tx_result = run_js_transform(transform_code, engine_jsons)
    if tx_result["status"] != "ok":
        return tx_result
    if not all(isinstance(row, dict) for row in tx_result["data"]):
        return {"status": "error", "error": "Chart transform must return an array of row objects"}

    # Build self-contained chart object
    chart = {
        "type": "chart",
        "chart_type": chart_type,
        "data": tx_result["data"],
        "x_key": chart_spec["x_key"],
        "y_keys": chart_spec["y_keys"],
        "y_scale": 0,
        "y_decimals": engine_decimals,
    }
    # Optional chart_spec fields (y_scale excluded — forced to 0 above)
    for opt in ("series_labels", "y_format", "stacked",
                 "title", "x_label", "y_label",
                 # Dual Y axis (line / bar / area only)
                 "y_axis_map", "y_format_right", "y_decimals_right",
                 "y_label_right",
                 # Scatter encodings (scatter only)
                 "size_key", "size_label", "color_key",
                 # forecast_line / cohort_heatmap / sparkline_row
                 "forecast_from", "value_key"):
        if opt in chart_spec:
            chart[opt] = chart_spec[opt]

    # Attach caption from engine result (period, scenarios, filters, scale, timestamp)
    if engine_caption and isinstance(engine_caption, dict):
        # Refresh timestamp to reflect chart creation, not original query
        from datetime import datetime, timezone
        caption = dict(engine_caption)
        caption["generated_at"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )
        chart["caption"] = caption

    # Generate a chart_ref for analysis fence embedding
    chart_ref = "chart_" + uuid.uuid4().hex

    output = {
        "status": "ok",
        "chart_ref": chart_ref,
        "chart": chart,
        "source": source,
        "transform_code": transform_code,
        "row_count": tx_result["row_count"],
        "execution_time_ms": tx_result["execution_time_ms"],
    }

    return output


def register_chart_tools(reg):
    reg.tool()(eval_chart_transform)
