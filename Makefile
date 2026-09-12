# dita — repo-level tasks. Per-service detail lives in each service's README.

OCR_DIR := services/inferences-ocr
COMPOSE := docker compose -f deployment/docker-compose.yml

.PHONY: help build test coverage lock lock-check lock-upgrade

help:
	@echo "build         build every service image"
	@echo "test          run the inferences-ocr test suite"
	@echo "coverage      run it under coverage and enforce the floor"
	@echo "lock          re-resolve uv.lock after editing a pyproject.toml"
	@echo "lock-check    fail if uv.lock is out of date (what the image build enforces)"
	@echo "lock-upgrade  re-resolve, allowing newer versions within the declared bounds"

build:
	$(COMPOSE) build

# uv run syncs the workspace from uv.lock first, so the suite runs against exactly the
# versions the image installs.
test:
	uv run --package inferences-ocr --locked --directory $(OCR_DIR) \
		python -m unittest discover -s tests -t .

# The floor sits just under what the suite actually reaches, so the engine adapters
# cannot quietly fall back towards 0% the way they were before they had unit tests.
# The number is a tripwire, not a target: a test that asserts nothing raises it just as
# well as a test that asserts something, so read the table, not the percentage.
COVERAGE_FLOOR := 83

coverage:
	uv run --package inferences-ocr --locked --directory $(OCR_DIR) \
		coverage run -m unittest discover -s tests -t .
	uv run --package inferences-ocr --locked --directory $(OCR_DIR) \
		coverage report --fail-under=$(COVERAGE_FLOOR)

lock:
	uv lock

# The same assertion the Dockerfile makes, available before you wait for a build. Edit a
# bound in a pyproject.toml without running `make lock` and this fails.
lock-check:
	uv lock --check

# Dependabot does this on a schedule; this is the manual equivalent.
lock-upgrade:
	uv lock --upgrade
