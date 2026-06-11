# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Unit tests for `_json_safe_inspection_result`.

Strict JSON-encoding regressions for the inspect_rows payload sanitiser.
NaN/Inf in a ToolMessage payload breaks both the browser SSE parser and
FastAPI's strict JSONResponse — see incident 2026-05-12.

Lifted from the legacy `tests/test_read_tools.py` as the only pure-unit
slice of that file. The remaining read-tool component tests live in
`tests/component/test_read_tools.py`.
"""

import pytest


class TestJsonSafeInspectionResult:

    def test_nan_inf_decimal_uuid_replaced(self):
        import json
        import uuid
        from decimal import Decimal

        from precis_mcp.tools.read_tools import _json_safe_inspection_result

        uid = uuid.uuid4()
        result = {
            "columns": ["a", "b", "c", "d", "e", "f"],
            "rows": [{
                "a": float("nan"),
                "b": float("inf"),
                "c": Decimal("1.50"),
                "d": uid,
                "e": None,
                "f": "ok",
            }],
        }
        cleaned = _json_safe_inspection_result(result)
        row = cleaned["rows"][0]
        assert row["a"] is None
        assert row["b"] is None
        assert row["c"] == 1.5
        assert row["d"] == str(uid)
        assert row["e"] is None
        assert row["f"] == "ok"
        # Strict JSON round-trip must succeed.
        json.dumps(cleaned, allow_nan=False)

    def test_pandas_na_and_nat_replaced(self):
        import json

        pd = pytest.importorskip("pandas")

        from precis_mcp.tools.read_tools import _json_safe_inspection_result

        result = {
            "rows": [{
                "supplier_id": pd.NA,
                "posting_date": pd.NaT,
                "amount": pd.Timestamp("2026-05-01"),
            }],
        }
        cleaned = _json_safe_inspection_result(result)
        row = cleaned["rows"][0]
        assert row["supplier_id"] is None
        assert row["posting_date"] is None
        # Timestamp is a datetime subclass → isoformat.
        assert isinstance(row["amount"], str)
        json.dumps(cleaned, allow_nan=False)

    def test_numpy_scalars_unwrapped(self):
        import json

        np = pytest.importorskip("numpy")

        from precis_mcp.tools.read_tools import _json_safe_inspection_result

        result = {
            "rows": [{
                "i": np.int64(42),
                "f": np.float64(3.5),
                "b": np.bool_(True),
                "nan": np.float64("nan"),
            }],
        }
        cleaned = _json_safe_inspection_result(result)
        row = cleaned["rows"][0]
        assert row["i"] == 42
        assert row["f"] == 3.5
        assert row["b"] is True
        assert row["nan"] is None
        json.dumps(cleaned, allow_nan=False)
