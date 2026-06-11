#!/usr/bin/env python3
"""Add the Elastic License 2.0 SPDX header to open-core Python files.

R1 (docs/precis_mcp_package_spec.md §4): every file in the open `precis-mcp`
distribution carries a short licence header. This is the scripted application.

Idempotent — files already carrying an `SPDX-License-Identifier` line are left
untouched, so it is safe to re-run (e.g. after new files land, or to extend the
target set at the R4 cut). The header is inserted after any shebang and/or
PEP 263 encoding cookie, before the module docstring.

Targets are passed as arguments (files or directories; directories are scanned
recursively for `*.py`). Pass only the OPEN tree — never the commercial
`precis/` package, which is not ELv2. With no arguments, defaults to the open
package `precis_mcp`.

    python scripts/add_license_headers.py precis_mcp deploy/keycloak/reconcile.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HEADER = (
    "# SPDX-License-Identifier: Elastic-2.0\n"
    "# Copyright (c) 2026 Sergio Naval Marimont\n"
)

_ENCODING_RE = re.compile(r"^[ \t\f]*#.*coding[:=]")


def _iter_py(targets: list[str]) -> list[Path]:
    out: list[Path] = []
    for t in targets:
        p = Path(t)
        if p.is_dir():
            out.extend(sorted(p.rglob("*.py")))
        elif p.suffix == ".py" and p.is_file():
            out.append(p)
    return out


def _apply(path: Path) -> bool:
    """Insert the header if absent. Returns True if the file was changed."""
    text = path.read_text(encoding="utf-8")
    if "SPDX-License-Identifier" in text[:512]:
        return False

    lines = text.splitlines(keepends=True)
    insert_at = 0
    # Preserve a shebang on line 0 and a PEP 263 encoding cookie on line 0/1.
    if lines and lines[0].startswith("#!"):
        insert_at = 1
    if len(lines) > insert_at and _ENCODING_RE.match(lines[insert_at]):
        insert_at += 1

    lines.insert(insert_at, HEADER)
    path.write_text("".join(lines), encoding="utf-8")
    return True


def main(argv: list[str]) -> int:
    targets = argv or ["precis_mcp"]
    files = _iter_py(targets)
    changed = sum(_apply(f) for f in files)
    print(f"Headered {changed} file(s); {len(files) - changed} already had it "
          f"(scanned {len(files)}).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
