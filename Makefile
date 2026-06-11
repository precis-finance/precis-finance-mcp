.PHONY: help test test-pre-push test-ci lint-imports check-imports

help:
	@echo "Targets:"
	@echo "  make test          Fast inner loop (~30s target): tests/unit only"
	@echo "  make test-pre-push Pre-push (~3min target): tests/unit + tests/component, excluding slow"
	@echo "  make test-ci       Full suite: everything including slow and e2e"
	@echo "  make lint-imports  Open-core boundary check via import-linter (monorepo; needs precis present)"
	@echo "  make check-imports Open-core boundary check via AST scan (no deps; the open-repo guard)"
	@echo ""
	@echo "See tests/CLAUDE.md for the test taxonomy and conventions."

test:
	pytest tests/unit -x -q

test-pre-push:
	pytest tests/unit tests/component -x -m "not slow"

test-ci:
	pytest

# Open-core boundary check for the precis-mcp split (docs/decisions/0003-*).
# Full import-graph analysis; requires the commercial `precis` package present,
# so it is the monorepo guard. In the open repo (precis absent) this contract
# degenerates — use `check-imports` there instead.
lint-imports:
	lint-imports --config pyproject.toml

# Dependency-free AST scan: no commercial `precis` import in precis_mcp/ or the
# open test manifest. Works with or without `precis` present, so it is the
# open-repo boundary guard (and a fast pre-cut check in the monorepo).
check-imports:
	python scripts/check_no_commercial_imports.py
