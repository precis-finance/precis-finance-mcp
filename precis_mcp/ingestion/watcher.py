# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Object-store watcher — turns new files in S3/SFTP/HTTPS-upload-landing
into `run_binding` calls.

One `Watcher` instance services every binding whose `schedule.mode == 'watch'`.
Each `tick()` lists candidate files, diffs against the set of file keys
already represented in `load_history` for that binding, and fires
`run_binding(binding_id, period, "watch:<key>")` for each new file.

The watcher does not own the polling cadence — `run_forever(interval)` is a
helper for a systemd service; tests call `tick()` directly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from precis_mcp.ingestion.file_readers import get_reader
from precis_mcp.ingestion.object_store import ObjectStoreClient
from precis_mcp.ingestion.orchestrator import (
    LoadAttemptResult,
    OrchestratorContext,
    run_binding,
)
from precis_mcp.ingestion.period_inference import (
    PeriodInferenceError,
    infer_period_from_filename,
    infer_period_from_rows,
)
from precis_mcp.ingestion.registry import Binding, Source
from precis_mcp.observability import get_logger

_logger = get_logger("ingestion.watcher")

# Matches the upload endpoint's cap; period inference fully materialises the
# file through a format reader, so it gets the same ceiling.
_MAX_INFERENCE_BYTES = 256 * 1024 * 1024


# `(source) -> ObjectStoreClient` — the wiring layer caches and reuses
# instances per source.
StoreFactory = Callable[[Source], ObjectStoreClient]


@dataclass
class WatcherTickResult:
    """One polling cycle's outcome — for telemetry and tests."""

    bindings_inspected: int = 0
    files_seen: int = 0
    files_new: int = 0
    loads_fired: int = 0
    loads_skipped_period_inference: int = 0
    attempts: list[LoadAttemptResult] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.attempts is None:
            self.attempts = []


class Watcher:
    """Polling watcher. Stateless across ticks — every cycle re-queries
    `load_history` for the processed set, so restarts don't replay files."""

    def __init__(
        self,
        ctx: OrchestratorContext,
        store_factory: StoreFactory,
    ) -> None:
        self.ctx = ctx
        self._store_factory = store_factory

    # -- Public API --------------------------------------------------------

    def tick(self) -> WatcherTickResult:
        result = WatcherTickResult()
        for binding in self.ctx.registry.bindings.values():
            if binding.schedule.mode != "watch":
                continue
            result.bindings_inspected += 1
            self._process_binding(binding, result)
        _logger.info(
            "ingest.watcher.tick.done",
            bindings_inspected=result.bindings_inspected,
            files_seen=result.files_seen,
            files_new=result.files_new,
            loads_fired=result.loads_fired,
            loads_skipped_period_inference=result.loads_skipped_period_inference,
        )
        return result

    def run_forever(self, interval_seconds: float, *, stop_after: Optional[int] = None) -> None:
        """Service entrypoint — invokes `tick()` every `interval_seconds`.

        `stop_after` bounds the number of ticks (test convenience); None runs
        indefinitely. Catches per-tick exceptions and logs them; the loop
        keeps going so one transient store outage doesn't kill the watcher.
        """
        ticks = 0
        while True:
            try:
                self.tick()
            except Exception:
                _logger.exception("ingest.watcher.tick.failed")
            ticks += 1
            if stop_after is not None and ticks >= stop_after:
                return
            time.sleep(interval_seconds)

    # -- Internal ----------------------------------------------------------

    def _process_binding(self, binding: Binding, result: WatcherTickResult) -> None:
        watch = binding.schedule.watch
        if watch is None:  # pragma: no cover — validated at registry load
            return
        source = self.ctx.registry.get_source(binding.source)
        store = self._store_factory(source)

        prefix = source.backend.get("prefix", "")
        try:
            file_metas = list(store.list_keys(prefix=prefix, glob=watch.file_glob))
        except Exception as exc:
            _logger.warning(
                "ingest.watcher.list_failed",
                binding_id=binding.id,
                source_id=source.id,
                error=str(exc),
            )
            return
        result.files_seen += len(file_metas)

        already_processed = self.ctx.load_history.processed_watch_keys_for_binding(
            binding.id
        )

        for meta in file_metas:
            if meta.key in already_processed:
                continue
            result.files_new += 1

            # Infer period — two supported modes:
            #   - filename_regex: parse the file name (zero-roundtrip)
            #   - column: peek into the file's first data row via the
            #     source's file-format reader
            try:
                period = self._infer_period(binding, source, store, meta)
            except PeriodInferenceError as exc:
                _logger.warning(
                    "ingest.watcher.period_inference_failed",
                    binding_id=binding.id,
                    key=meta.key,
                    period_from=watch.period_from,
                    error=str(exc),
                )
                result.loads_skipped_period_inference += 1
                continue

            triggered_by = f"watch:{meta.key}"
            _logger.info(
                "ingest.watcher.file_new",
                binding_id=binding.id,
                key=meta.key,
                period=period,
            )
            attempt = run_binding(self.ctx, binding.id, period, triggered_by)
            result.loads_fired += 1
            result.attempts.append(attempt)

    def _infer_period(
        self,
        binding: Binding,
        source: Source,
        store: ObjectStoreClient,
        meta,
    ) -> str:
        """Return the canonical 'YYYY-MM' period for one new file.

        Strategy is `binding.schedule.watch.period_from`:
          - `filename_regex`: parse from the file name only (cheap).
          - `column`: download the file, parse with the format reader,
            read `watch.period_column` on the first data row.
        """
        watch = binding.schedule.watch
        assert watch is not None
        if watch.period_from == "filename_regex":
            if not watch.filename_regex:
                raise PeriodInferenceError(
                    "watch.period_from='filename_regex' requires watch.filename_regex"
                )
            return infer_period_from_filename(
                meta.key.rsplit("/", 1)[-1], watch.filename_regex
            )
        if watch.period_from == "column":
            if not watch.period_column:
                raise PeriodInferenceError(
                    "watch.period_from='column' requires watch.period_column"
                )
            return self._infer_period_from_file_column(
                store, meta.key, source, binding, watch.period_column
            )
        raise PeriodInferenceError(
            f"unknown watch.period_from: {watch.period_from!r}"
        )

    def _infer_period_from_file_column(
        self,
        store: ObjectStoreClient,
        key: str,
        source: Source,
        binding: Binding,
        period_column: str,
    ) -> str:
        """Read the file's first data row via the source's format reader and
        derive the period from `period_column`.

        Today this reads the entire file (small files in practice; GL exports
        are tens of thousands of rows). Streaming-header support is a future
        optimisation for the multi-GB case.
        """
        file_format = source.backend.get("file_format")
        if not file_format:
            raise PeriodInferenceError(
                f"source.backend.file_format missing for source {source.id!r}"
            )
        try:
            reader = get_reader(file_format)
        except KeyError as exc:
            raise PeriodInferenceError(
                f"no reader for file_format={file_format!r}"
            ) from exc

        format_config = source.backend.get(file_format, {})
        body = store.get_bytes(key)
        if len(body) > _MAX_INFERENCE_BYTES:
            # The readers fully materialise the file (and xlsx decompresses);
            # refuse to parse an oversized drop just to peek at one row.
            raise PeriodInferenceError(
                f"file {key!r} is {len(body)} bytes — exceeds the "
                f"{_MAX_INFERENCE_BYTES}-byte period-inference cap"
            )
        # Read with column_map=identity — we only need the raw period_column;
        # the eventual driver will read again with the binding's column_map.
        rows = reader.read(
            body,
            format_config=format_config,
            column_map="identity",
        )
        return infer_period_from_rows(rows, period_column)
