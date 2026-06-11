#!/usr/bin/env python3
# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Open-core boundary check — no commercial (`precis`) imports.

Replaces the ``lint-imports`` forbidden contract in the open ``precis-mcp`` repo,
where that contract degenerates: with the commercial ``precis`` package absent
there is no module to forbid. This is a self-contained AST scan — no third-party
dependency — so CI can run it before installing anything.

Scans the open package (``precis_mcp/``) and every file in the open test
manifest (``tests/open_tests.txt``). Fails if any imports the commercial
``precis`` package (``precis`` or ``precis.*``). ``precis_mcp`` is the open
package and is excluded by construction — it does not start with ``precis.``.

In the monorepo this complements ``make lint-imports`` (import-linter does the
richer full-graph analysis while ``precis`` is present); in the open repo it is
the sole boundary guard. Usage::

    python scripts/check_no_commercial_imports.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _imports(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _commercial(names: set[str]) -> set[str]:
    return {n for n in names if n == "precis" or n.startswith("precis.")}


def _open_test_files() -> list[Path]:
    manifest = ROOT / "tests" / "open_tests.txt"
    if not manifest.exists():
        return []
    files: list[Path] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            files.append(ROOT / "tests" / line)
    return files


def main() -> int:
    targets = sorted((ROOT / "precis_mcp").rglob("*.py")) + _open_test_files()
    violations: dict[Path, list[str]] = {}
    for path in targets:
        bad = _commercial(_imports(path))
        if bad:
            violations[path] = sorted(bad)

    if violations:
        print("Commercial `precis` imports in open-core files:", file=sys.stderr)
        for path, mods in violations.items():
            print(f"  {path.relative_to(ROOT)}: {', '.join(mods)}", file=sys.stderr)
        print(
            "\nThe open package must not import the commercial `precis` package. "
            "Use the `precis_mcp` equivalent, or (for a test) remove it from "
            "tests/open_tests.txt if it is genuinely commercial.",
            file=sys.stderr,
        )
        return 1

    print(f"OK — no commercial `precis` imports across {len(targets)} open-core files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())