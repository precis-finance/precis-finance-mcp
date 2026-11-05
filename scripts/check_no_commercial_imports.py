#!/usr/bin/env python3
# SPDX-License-Identifier: Elastic-2.0
# Copyright (c) 2026 Sergio Naval Marimont
"""Open-core boundary check — no commercial (`precis`) imports.

Replaces the ``lint-imports`` forbidden contract in the open ``precis-mcp`` repo,
where that contract degenerates: with the commercial ``precis`` package absent
there is no module to forbid. This is a self-contained AST scan — no third-party
dependency — so CI can run it before installing anything.

Scans the package, test manifest and every helper in ``tests/open_support.txt``.
Imports of the commercial ``precis`` package or undeclared test helpers fail
the check. No test modules or optional runtime dependencies are imported.

In the monorepo this complements ``make lint-imports`` (import-linter does the
richer full-graph analysis while ``precis`` is present); in the open repo it is
the sole boundary guard. Usage::

    python scripts/check_no_commercial_imports.py
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.open_test_manifest import public_test_layout


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


def boundary_findings(root: Path, *, published: bool) -> tuple[int, dict[Path, list[str]]]:
    """Check public code and test support without importing any test modules."""
    entries, test_paths = public_test_layout(root, published=published)
    tests = [root / "tests" / path for path in test_paths]
    support = [root / "tests" / (destination if published else source)
               for source, destination in entries if destination.endswith(".py")]
    expected_tests = set(tests) | set(support)
    actual_tests = set((root / "tests").rglob("*.py")) if published else expected_tests
    targets = sorted(set((root / "precis_mcp").rglob("*.py")) | expected_tests | actual_tests)
    destinations = {root / "tests" / source: destination for source, destination in entries}
    public_modules = {"tests"}
    relative_paths = [str(path.relative_to(root)) for path in tests]
    relative_paths.extend(f"tests/{destination}" for _, destination in entries)
    for relative in relative_paths:
        path = Path(relative)
        if path.suffix == ".py":
            parts = list(path.with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            public_modules.add(".".join(parts))
    violations: dict[Path, list[str]] = {}
    for path in targets:
        if published and path in actual_tests and path not in expected_tests:
            violations[path] = ["undeclared public test/support file"]
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            violations[path] = ["missing, unreadable or invalid public source"]
            continue
        relative = Path("tests") / destinations[path] if not published and path in destinations else path.relative_to(root)
        package = list(relative.with_suffix("").parts)
        if package[-1] == "__init__":
            package.pop()
        else:
            package = package[:-1]
        imports = set()
        import_from = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                name = node.module or ""
                if node.level:
                    base = package[:len(package) - node.level + 1]
                    name = ".".join([*base, *name.split(".")]) if name else ".".join(base)
                imports.add(name)
                import_from.append((name, node.names))
        bad = _commercial(imports)
        bad.update(name for name in imports if name.startswith("tests.") and name not in public_modules)
        for name, aliases in import_from:
            package_path = root.joinpath(*name.split("."))
            if name in public_modules and (package_path / "__init__.py").is_file():
                for alias in aliases:
                    module = f"{name}.{alias.name}"
                    if module not in public_modules:
                        bad.add(module)
        if bad:
            violations[path] = sorted(bad)
    return len(targets), violations


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the public package and all exported test support.")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--published", action="store_true", help="Check paths in an assembled export")
    args = parser.parse_args()
    root = args.root.resolve()
    published = args.published or not (root / "pyproject-open.toml").is_file()
    try:
        count, violations = boundary_findings(root, published=published)
    except (OSError, ValueError) as error:
        print(f"Invalid public-test manifest: {error}", file=sys.stderr)
        return 1

    if violations:
        print("Imports outside the public package/test boundary:", file=sys.stderr)
        for path, mods in violations.items():
            print(f"  {path.relative_to(root)}: {', '.join(mods)}", file=sys.stderr)
        print(
            "\nPublic tests and helpers must depend only on the public package "
            "and the helpers declared in tests/open_support.txt.",
            file=sys.stderr,
        )
        return 1

    print(f"OK — public import boundary holds across {count} package/test files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
