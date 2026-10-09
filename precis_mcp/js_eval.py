# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Lightweight JS evaluator for chart transform functions.

Uses quickjs-ng to execute agent-generated JavaScript transform functions
in an isolated context with memory and time limits.  No filesystem, no
network, no imports — only pure JS operating on a JSON input.

The ``run_js_transform`` helper is the reusable core — called by
``chart_tools.eval_chart_transform`` and available for any future path
that needs sandboxed JS execution (e.g. report refresh workers).
"""

import json
import logging
import time

logger = logging.getLogger(__name__)

# Limits ------------------------------------------------------------------
_MEMORY_LIMIT = 16 * 1024 * 1024   # 16 MB
_TIME_LIMIT = 5.0                   # seconds
_MAX_STACK_SIZE = 512 * 1024        # 512 KB
_OUTPUT_MAX = 256 * 1024            # 256 KB — reject oversized output


def validate_transform_code(transform_code: str) -> str | None:
    """Structurally validate a transform at author time — data-independent.

    Confirms the code parses and evaluates to a **function**, without running
    it against any data. Returns ``None`` when valid, else a human-readable
    error. Call this on the write path (chart authoring) so a malformed
    transform is rejected when it is created rather than silently failing on
    every later report refresh.

    The transform is evaluated in *argument position* inside a throwaway
    sandbox (`(function(__f){…})((<code>))`): passing it as an argument keeps
    it in an expression context, so a statement-injection attempt
    (`x=>x); doThing(); (y=>y`) is a `SyntaxError`, not executed code. The
    sandbox itself has no host bindings, no I/O, and bounded memory/time —
    evaluating the *expression* only constructs the function value; the
    function body never runs here.
    """
    if not transform_code or not transform_code.strip():
        return "No transform code provided"

    import quickjs  # defer import to avoid hard dep at module load

    ctx = quickjs.Context()
    ctx.set_memory_limit(_MEMORY_LIMIT)
    ctx.set_time_limit(_TIME_LIMIT)
    ctx.set_max_stack_size(_MAX_STACK_SIZE)
    try:
        is_fn = ctx.eval(
            "(function(__f){ return typeof __f === 'function'; })"
            f"(({transform_code}))"
        )
    except Exception as exc:
        first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return f"transform_code is not valid JavaScript: {first_line}"
    if not is_fn:
        return (
            "transform_code must be a function expression, e.g. "
            "(input) => input.rows.map(r => ({...}))"
        )
    return None


def run_js_transform(
    transform_code: str, engine_result_json: str | list[str]
) -> dict:
    """Execute a JS transform function against one or more engine results.

    This is the reusable core shared by chart_tools.eval_chart_transform
    and the report-refresh path.

    The transform is called variadically: each engine result is passed as a
    positional argument in order. A single input (``str``) is therefore the
    one-argument case of the N-argument form — ``(input) => ...`` keeps
    working unchanged, while a multi-input chart writes
    ``(fin, util) => ...`` and aligns the two pre-aggregated series on a
    shared dimension. (No SUM crosses the join, so no fan-out; when a finer
    point has no match in a coarser series, emit ``null`` — never repeat the
    coarse value down to invalid detail.)

    Args:
        transform_code: JavaScript arrow/function expression.
            ``(input) => input.rows.map(r => ...)`` or
            ``(a, b) => a.rows.map(...)`` for multiple inputs.
        engine_result_json: Raw JSON string of one engine output, or a list
            of such strings (passed to the transform in order).

    Returns:
        On success: ``{"status": "ok", "data": [...], "row_count": N,
                       "execution_time_ms": N}``
        On error:   ``{"status": "error", "error": "...",
                       "execution_time_ms": N}``
    """
    if not transform_code or not transform_code.strip():
        return {"status": "error", "error": "No transform code provided",
                "execution_time_ms": 0}

    inputs = (
        [engine_result_json]
        if isinstance(engine_result_json, str)
        else list(engine_result_json)
    )
    if not inputs or not all(j and j.strip() for j in inputs):
        return {"status": "error", "error": "No engine result provided",
                "execution_time_ms": 0}

    # Validate JSON before sending to JS
    for idx, j in enumerate(inputs):
        try:
            json.loads(j)
        except (json.JSONDecodeError, TypeError) as exc:
            return {"status": "error",
                    "error": f"engine_result[{idx}] is not valid JSON: {exc}",
                    "execution_time_ms": 0}

    import quickjs  # defer import to avoid hard dep at module load

    ctx = quickjs.Context()
    ctx.set_memory_limit(_MEMORY_LIMIT)
    ctx.set_time_limit(_TIME_LIMIT)
    ctx.set_max_stack_size(_MAX_STACK_SIZE)

    t0 = time.monotonic()
    try:
        # Inject the engine results as a global array, then spread them as
        # positional args. Two-step eval avoids string-escaping issues.
        # Single input → one-arg call, identical to the legacy behaviour.
        array_literal = "[" + ",".join(inputs) + "]"
        ctx.eval(f"const __inputs = {array_literal};")
        # Pass the transform as an ARGUMENT to a checker (expression position),
        # so a statement-injection attempt is a SyntaxError rather than
        # executed code, and a non-function is rejected with a clear message
        # before it is ever invoked. (The QuickJS context has no host bindings
        # or I/O — this hardening is about legibility and injection-shape
        # resistance, not escaping an otherwise-open sandbox.)
        raw = ctx.eval(
            "(function(__f){"
            " if (typeof __f !== 'function')"
            "   throw new TypeError('transform_code must evaluate to a function');"
            " return JSON.stringify(__f(...__inputs));"
            f"}})(({transform_code}))"
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        if raw is None:
            return {"status": "error",
                    "error": "Transform returned undefined/null",
                    "execution_time_ms": elapsed_ms}

        output = str(raw)
        output_bytes = len(output.encode("utf-8"))
        if output_bytes > _OUTPUT_MAX:
            return {"status": "error",
                    "error": f"Output too large ({output_bytes} bytes, "
                             f"limit {_OUTPUT_MAX})",
                    "execution_time_ms": elapsed_ms}

        data = json.loads(output)

        if not isinstance(data, list):
            return {"status": "error",
                    "error": f"Transform must return an array, "
                             f"got {type(data).__name__}",
                    "execution_time_ms": elapsed_ms}

        return {
            "status": "ok",
            "data": data,
            "row_count": len(data),
            "execution_time_ms": elapsed_ms,
        }

    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        error_msg = str(exc)
        return {"status": "error", "error": error_msg,
                "execution_time_ms": elapsed_ms}
