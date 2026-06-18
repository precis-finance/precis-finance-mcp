# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Shared result-shape types for the metric engine pipeline.

Defined once here and imported by the retriever (Stage 3), transformer
(Stage 4), and formatter (Stage 5) so the pipeline's intermediate contract
has a single definition.
"""
from __future__ import annotations

# Tuple of dimension values — e.g. () for aggregate, ('2025-01',) for monthly,
# ('2025-01', 'CC-SE') for multi-dim.
DimensionKey = tuple[str, ...]

# Sentinel occupying a DimensionKey position whose dimension was rolled up in a
# subtotal or grand-total grain. Kept distinct from any real (default-filled)
# dimension value so different grains never collide as dict keys.
ROLLED_UP = "\x00__rolled_up__"

# scenario_key -> dimension_key -> metric_key -> value
ResultData = dict[str, dict[DimensionKey, dict[str, float | None]]]

# Same shape as ResultData; named distinctly to mark the retriever's raw,
# pre-transform output from the transformed result the later stages consume.
RawResults = ResultData
