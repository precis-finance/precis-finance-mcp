# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Pure inspection-grid block projection tests."""

from precis_mcp.inspection_grid_builder import build_inspection_grid_block


def test_grid_keeps_preview_bounded_and_passes_optional_data_ref():
    rows = [{"transaction_id": f"T-{index}"} for index in range(100)]

    block = build_inspection_grid_block({
        "source_key": "transactions",
        "columns": ["transaction_id"],
        "rows": rows,
        "row_count": 8_000,
        "limit": 10_000,
        "truncated": False,
        "data_ref": "conversation:inspection",
    })

    assert block is not None
    assert len(block["rows"]) == 50
    assert block["row_count"] == 8_000
    assert block["preview_truncated"] is True
    assert block["source_truncated"] is False
    assert block["truncated"] is True
    assert block["data_ref"] == "conversation:inspection"


def test_grid_open_result_has_no_cache_capability_and_marks_source_cap():
    block = build_inspection_grid_block({
        "source_key": "transactions",
        "columns": ["transaction_id"],
        "rows": [{"transaction_id": "T-1"}],
        "row_count": 1,
        "limit": 1,
        "truncated": True,
    })

    assert block is not None
    assert "data_ref" not in block
    assert block["source_truncated"] is True
    assert block["truncated"] is True
