# Repo-level tasks. Per-service tasks live in services/<name>/Makefile; this delegates.

SERVICES := services/inferences-ocr
PY_UNITS := packages/pylibs/dip packages/pylibs/worker
COMPOSE  := docker compose -f deployment/docker-compose.yml
SCHEMA   := specs/dip/dip.schema.json

# Dev-only, pinned, never a runtime dependency of anything.
PY_CODEGEN := datamodel-code-generator==0.40.0

.PHONY: help doctor test coverage build lock lock-check lock-upgrade \
        py-test py-coverage py-verify dip-generate dip-corpus

help:
	@echo "doctor        check this machine has the tools this repo needs"
	@echo "test          run every service's tests"
	@echo "coverage      run every service's coverage, reported per service"
	@echo "build         build every service image"
	@echo "lock          re-resolve uv.lock after editing a pyproject.toml"
	@echo "lock-check    fail if uv.lock is out of date (what the image build enforces)"
	@echo "lock-upgrade  re-resolve, allowing newer versions within the declared bounds"
	@echo "py-test       run every Python package's tests"
	@echo "py-coverage   run every Python package's coverage against its floor"
	@echo "py-verify     prove the Python packages have no unexpected dependencies"
	@echo "dip-generate  regenerate the Python DIP types from the IDL"
	@echo "dip-corpus    regenerate the conformance corpus and validate it"
	@echo ""
	@echo "services: $(SERVICES)"
	@echo "packages: $(PY_UNITS)"

doctor:
	@./scripts/doctor.sh

test:
	@for service in $(SERVICES); do \
		echo "== $$service"; \
		$(MAKE) --no-print-directory -C $$service test || exit $$?; \
	done

# Each service reports its own number against its own code. Never blended: a healthy
# service would otherwise mask an untested one.
coverage:
	@for service in $(SERVICES); do \
		echo "== $$service"; \
		$(MAKE) --no-print-directory -C $$service coverage || exit $$?; \
	done

build:
	$(COMPOSE) build

lock:
	uv lock

lock-check:
	uv lock --check

lock-upgrade:
	uv lock --upgrade

py-test:
	@for unit in $(PY_UNITS); do \
		echo "== $$unit"; \
		$(MAKE) --no-print-directory -C $$unit test || exit $$?; \
	done

py-coverage:
	@for unit in $(PY_UNITS); do \
		echo "== $$unit"; \
		$(MAKE) --no-print-directory -C $$unit coverage || exit $$?; \
	done

# The acceptance criterion for the generated code: a runtime library smuggled in by a
# generator would be a dependency nobody chose. yaml is the manifest reader's one
# dependency, named here so it stays a decision rather than a drift.
py-verify:
	@echo "== packages/pylibs/dip"
	@uv run --frozen python scripts/check_stdlib_only.py packages/pylibs/dip/src
	@echo "== packages/pylibs/worker"
	@uv run --frozen python scripts/check_stdlib_only.py packages/pylibs/worker/src --allow yaml

dip-generate:
	uv run --frozen --with $(PY_CODEGEN) datamodel-codegen \
		--input $(SCHEMA) --input-file-type jsonschema \
		--output-model-type dataclasses.dataclass --target-python-version 3.14 \
		--disable-timestamp \
		--output packages/pylibs/dip/src/dip/types.py

dip-corpus:
	uv run --frozen python specs/dip/conformance/build_corpus.py
