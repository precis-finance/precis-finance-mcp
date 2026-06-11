# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Tests for object_store.py (InMemory, LocalFs) and period_inference.py.

S3 and SFTP implementations are not tested here — they require boto3 /
paramiko which are not in `requirements.txt`. Their behaviour is covered by
contract: any class satisfying `ObjectStoreClient` works with the file-drop
driver.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from precis_mcp.ingestion.object_store import (
    InMemoryObjectStore,
    LocalFsObjectStore,
)
from precis_mcp.ingestion.period_inference import (
    PeriodInferenceError,
    infer_period_from_filename,
    infer_period_from_rows,
    normalise_period,
)


# ---------------------------------------------------------------------------
# InMemoryObjectStore
# ---------------------------------------------------------------------------


def test_in_memory_put_and_get():
    store = InMemoryObjectStore()
    store.put("a.csv", b"hello")
    assert store.get_bytes("a.csv") == b"hello"


def test_in_memory_list_keys_default_returns_all():
    store = InMemoryObjectStore()
    store.put("a.csv", b"1")
    store.put("b.csv", b"2")
    metas = list(store.list_keys())
    assert [m.key for m in metas] == ["a.csv", "b.csv"]


def test_in_memory_list_keys_with_prefix():
    store = InMemoryObjectStore()
    store.put("in/a.csv", b"1")
    store.put("out/b.csv", b"2")
    metas = list(store.list_keys(prefix="in/"))
    assert [m.key for m in metas] == ["in/a.csv"]


def test_in_memory_list_keys_with_glob():
    store = InMemoryObjectStore()
    store.put("gl_2026-04.csv", b"")
    store.put("ap_2026-04.csv", b"")
    metas = list(store.list_keys(glob="gl_*.csv"))
    assert [m.key for m in metas] == ["gl_2026-04.csv"]


def test_in_memory_get_missing_raises_file_not_found():
    store = InMemoryObjectStore()
    with pytest.raises(FileNotFoundError):
        store.get_bytes("ghost.csv")


def test_in_memory_delete():
    store = InMemoryObjectStore()
    store.put("x", b"1")
    store.delete("x")
    with pytest.raises(FileNotFoundError):
        store.get_bytes("x")


def test_in_memory_modified_timestamp_respected():
    store = InMemoryObjectStore()
    t = datetime(2026, 4, 1, tzinfo=timezone.utc)
    store.put("a", b"1", modified=t)
    metas = list(store.list_keys())
    assert metas[0].modified == t


# ---------------------------------------------------------------------------
# LocalFsObjectStore
# ---------------------------------------------------------------------------


def test_local_fs_round_trip(tmp_path: Path):
    store = LocalFsObjectStore(tmp_path)
    store.put("a.csv", b"hello")
    assert store.get_bytes("a.csv") == b"hello"


def test_local_fs_list_keys_finds_files(tmp_path: Path):
    store = LocalFsObjectStore(tmp_path)
    store.put("a.csv", b"1")
    store.put("nested/b.csv", b"2")
    keys = sorted(m.key for m in store.list_keys())
    assert keys == ["a.csv", "nested/b.csv"]


def test_local_fs_glob(tmp_path: Path):
    store = LocalFsObjectStore(tmp_path)
    store.put("gl_2026-04.csv", b"")
    store.put("ap_2026-04.csv", b"")
    keys = [m.key for m in store.list_keys(glob="gl_*.csv")]
    assert keys == ["gl_2026-04.csv"]


def test_local_fs_get_missing_raises(tmp_path: Path):
    store = LocalFsObjectStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.get_bytes("ghost.csv")


# ---------------------------------------------------------------------------
# normalise_period
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-04", "2026-04"),
        ("2026-04", "2026-04"),
        ("2026/04", "2026-04"),
        ("2026-04-01", "2026-04"),
        (" 2026-04 ", "2026-04"),
    ],
)
def test_normalise_period(raw: str, expected: str):
    assert normalise_period(raw) == expected


@pytest.mark.parametrize("bad", ["", "2026", "20260415x", "April 2026"])
def test_normalise_period_rejects_bad(bad: str):
    with pytest.raises(PeriodInferenceError):
        normalise_period(bad)


# ---------------------------------------------------------------------------
# infer_period_from_filename
# ---------------------------------------------------------------------------


def test_infer_period_from_filename_yyyymm():
    assert infer_period_from_filename(
        "gl_2026-04.csv", r"gl_(?P<period>\d{4}-\d{2})\.csv"
    ) == "2026-04"


def test_infer_period_from_filename_dash():
    assert infer_period_from_filename(
        "actuals-2026-04.parquet", r"actuals-(?P<period>\d{4}-\d{2})\.parquet"
    ) == "2026-04"


def test_infer_period_from_filename_no_match():
    with pytest.raises(PeriodInferenceError, match="did not match"):
        infer_period_from_filename("garbage.csv", r"gl_(?P<period>\d{4}-\d{2})\.csv")


def test_infer_period_from_filename_no_named_group():
    with pytest.raises(PeriodInferenceError, match="named group"):
        infer_period_from_filename("gl_2026-04.csv", r"gl_(\d{4}-\d{2})\.csv")


# ---------------------------------------------------------------------------
# infer_period_from_rows
# ---------------------------------------------------------------------------


def test_infer_period_from_rows_first_row():
    rows = [
        {"posting_date": "2026-04-15", "amount": 100},
        {"posting_date": "2026-04-20", "amount": 200},
    ]
    assert infer_period_from_rows(rows, "posting_date") == "2026-04"


def test_infer_period_from_rows_empty_stream_raises():
    with pytest.raises(PeriodInferenceError, match="empty"):
        infer_period_from_rows(iter([]), "posting_date")


def test_infer_period_from_rows_missing_column_raises():
    with pytest.raises(PeriodInferenceError, match="not found"):
        infer_period_from_rows(
            [{"other_col": "x"}], "posting_date"
        )


def test_infer_period_from_rows_null_value_raises():
    with pytest.raises(PeriodInferenceError, match="null"):
        infer_period_from_rows(
            [{"posting_date": None}], "posting_date"
        )
