.PHONY: help help-common test test-pre-push test-ci check-imports lock lock-extras lock-extras-open

help: help-common

help-common:
	@echo "Targets:"
	@echo "  make test          Fast inner loop (~30s target): tests/unit only"
	@echo "  make test-pre-push Pre-push (~3min target): tests/unit + tests/component, excluding slow"
	@echo "  make test-ci       Full suite: everything including slow and e2e"
	@echo "  make check-imports Open-core boundary check via AST scan (no deps; the open-repo guard)"
	@echo "  make lock          Regenerate the hashed dependency lockfile from the package metadata"
	@echo "  make lock-extras   Regenerate the version-pinned optional-warehouse-driver constraints"

test:
	pytest tests/unit -x -q

test-pre-push:
	pytest tests/unit tests/component -x -m "not slow"

test-ci:
	pytest

# Check public package and test imports without additional dependencies.
check-imports:
	python scripts/check_no_commercial_imports.py

# Generate with Python 3.12 (the image's interpreter) and pip-tools installed.
# pip-compile detects metadata from the literal filename pyproject.toml.
PYPROJECT ?= pyproject.toml
LOCKFILE ?= requirements.lock

lock:
	@t=$$(mktemp -d) && cp $(PYPROJECT) $$t/pyproject.toml && \
	pip-compile --quiet --no-header --generate-hashes --strip-extras \
		--output-file $(LOCKFILE) $$t/pyproject.toml && \
	rm -rf $$t && echo "wrote $(LOCKFILE)"

# Optional warehouse constraints share dependency versions with the core lock.
# Regenerate after `make lock`; consumed by the image's PRECIS_EXTRAS layer.
WAREHOUSE_EXTRAS := bigquery snowflake mssql databricks databricks-primary
lock-extras: lock-extras-open

lock-extras-open:
	@set -e; t=$$(mktemp -d); trap 'rm -rf "$$t"' EXIT; \
	cp "$(PYPROJECT)" "$$t/pyproject.toml"; \
	pip-compile --quiet --no-header --strip-extras \
		--constraint $(LOCKFILE) \
		$(foreach e,$(WAREHOUSE_EXTRAS),--extra $(e)) \
		--output-file constraints-extras.txt "$$t/pyproject.toml"; \
	echo "wrote constraints-extras.txt"
