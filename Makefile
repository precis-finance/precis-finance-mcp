.PHONY: help test test-pre-push test-ci lint-imports check-imports lock lock-extras

help:
	@echo "Targets:"
	@echo "  make test          Fast inner loop (~30s target): tests/unit only"
	@echo "  make test-pre-push Pre-push (~3min target): tests/unit + tests/component, excluding slow"
	@echo "  make test-ci       Full suite: everything including slow and e2e"
	@echo "  make lint-imports  Open boundary check via import-linter (Précis monorepo; needs precis present)"
	@echo "  make check-imports Open-core boundary check via AST scan (no deps; the open-repo guard)"
	@echo "  make lock          Regenerate the hashed dependency lockfile from the package metadata"
	@echo "  make lock-extras   Regenerate the version-pinned optional-warehouse-driver constraints (both images)"

test:
	pytest tests/unit -x -q

test-pre-push:
	pytest tests/unit tests/component -x -m "not slow"

test-ci:
	pytest

# Open-core boundary check for the precis-finance-mcp split.
# Full import-graph analysis; requires the `precis` package present,
# so it is the Précis monorepo guard. In the open repo (precis absent) this contract
# degenerates — use `check-imports` there instead.
lint-imports:
	lint-imports --config pyproject.toml

# Dependency-free AST scan: no `precis` import in precis_mcp/ or the
# open test manifest. Works with or without `precis` present, so it is the
# open-repo boundary guard (and a fast pre-cut check in the Précis monorepo).
check-imports:
	python scripts/check_no_commercial_imports.py

# Hashed pip lockfile for the Docker image's dependency layer. Works in both
# repos: the Précis monorepo stages pyproject-open.toml → requirements-open.lock; the
# `precis-finance-mcp` open repo compiles pyproject.toml → requirements.lock. Run under Python 3.12
# (the image's interpreter) with pip-tools installed (in the dev extra), then
# commit the result. The copy-to-tempdir dance exists because pip-compile
# detects the input format from the literal filename `pyproject.toml`.
PYPROJECT := $(if $(wildcard pyproject-open.toml),pyproject-open.toml,pyproject.toml)
LOCKFILE  := $(if $(wildcard pyproject-open.toml),requirements-open.lock,requirements.lock)

lock:
	@t=$$(mktemp -d) && cp $(PYPROJECT) $$t/pyproject.toml && \
	pip-compile --quiet --no-header --generate-hashes --strip-extras \
		--output-file $(LOCKFILE) $$t/pyproject.toml && \
	rm -rf $$t && echo "wrote $(LOCKFILE)"

# Version-pinned (NOT hashed) constraints for the optional warehouse drivers,
# one file per image base. The optional connector is the industry norm
# (dbt/Grafana/Airflow version-pin their drivers; pip --require-hashes is
# all-or-nothing across the transitive closure, so a hashed base cannot carry an
# unhashed bolt-on). Each constraints file must carry the SAME shared-dep
# versions as the base it layers onto, so regenerate this whenever `lock` or the
# the Précis platform requirements.txt changes. Consumed by Dockerfile.open / Dockerfile
# (PRECIS_EXTRAS build arg).
WAREHOUSE_EXTRAS := bigquery snowflake mssql databricks
lock-extras:
	@t=$$(mktemp -d) && cp pyproject-open.toml $$t/pyproject.toml && \
	pip-compile --quiet --no-header --no-strip-extras \
		$(foreach e,$(WAREHOUSE_EXTRAS),--extra $(e)) \
		--output-file constraints-extras.txt $$t/pyproject.toml && \
	rm -rf $$t && echo "wrote constraints-extras.txt (open base)"
	@t=$$(mktemp -d) && cat requirements.in > $$t/requirements.in && \
	echo 'ibis-framework[bigquery,snowflake,mssql,databricks]' >> $$t/requirements.in && \
	pip-compile --quiet --no-header --no-strip-extras -c requirements.txt \
		--output-file constraints-extras-commercial.txt $$t/requirements.in && \
	rm -rf $$t && echo "wrote constraints-extras-commercial.txt (Précis base)"
