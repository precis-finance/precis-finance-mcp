# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Read the explicit public-test support allowlist without importing test code."""
from __future__ import annotations

from pathlib import Path, PurePosixPath


def _python_path(value: str, manifest: Path, number: int) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value or path.suffix != ".py":
        raise ValueError(f"{manifest}:{number}: expected a normalized relative Python file path")
    return value


def support_entries(root: Path) -> list[tuple[str, str]]:
    """Return source/destination paths relative to tests/."""
    manifest = root / "tests" / "open_support.txt"
    entries = []
    destinations = set()
    for number, raw in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        fields = raw.split("#", 1)[0].split()
        if not fields:
            continue
        if len(fields) not in (1, 2):
            raise ValueError(f"{manifest}:{number}: expected source and optional destination")
        source, destination = fields[0], fields[-1]
        for field in (source, destination):
            _python_path(field, manifest, number)
        if destination in destinations:
            raise ValueError(f"{manifest}:{number}: duplicate destination {destination}")
        destinations.add(destination)
        entries.append((source, destination))
    return entries


def test_entries(root: Path) -> list[str]:
    """Read test-case paths with the same validation as reusable support."""
    manifest = root / "tests/open_tests.txt"
    entries = []
    for number, raw in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        fields = raw.split("#", 1)[0].split()
        if not fields:
            continue
        if len(fields) != 1:
            raise ValueError(f"{manifest}:{number}: expected one Python file path")
        path = _python_path(fields[0], manifest, number)
        if path in entries:
            raise ValueError(f"{manifest}:{number}: duplicate test {path}")
        entries.append(path)
    return entries


def public_test_layout(root: Path, *, published: bool = False, require_files: bool = False):
    """Validate both allowlists, collisions and resolved source containment."""
    support, tests = support_entries(root), test_entries(root)
    destinations = {destination for _, destination in support}
    overlap = destinations.intersection(tests)
    if overlap:
        raise ValueError(f"Test/support destination collision: {sorted(overlap)}")
    test_root = (root / "tests").resolve()
    paths = [destination if published else source for source, destination in support] + tests
    for relative in paths:
        path = test_root / relative
        if not path.resolve().is_relative_to(test_root):
            raise ValueError(f"Public test source escapes tests/: {relative}")
        if require_files and not path.is_file():
            raise ValueError(f"Public test/support source is missing or not a file: {relative}")
    return support, tests
