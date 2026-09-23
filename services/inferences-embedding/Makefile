PACKAGE   := inferences-embedding
REPO_ROOT := $(CURDIR)/../..
UV        := uv run --package $(PACKAGE) --locked

# A tripwire, not a target: set just below what the suite reaches.
COVERAGE_FLOOR := 93

.PHONY: help test coverage run

help:
	@echo "test      run the test suite"
	@echo "coverage  run the suite under coverage and enforce the $(COVERAGE_FLOOR)% floor"
	@echo "run       start the worker against ./models and ../../run, TEI on 127.0.0.1:8080"

test:
	$(UV) python -m unittest discover -s tests -t .

coverage:
	$(UV) coverage run -m unittest discover -s tests -t .
	$(UV) coverage report --fail-under=$(COVERAGE_FLOOR)

run:
	MODELS_DIR=./models SOCKET_PATH=$(REPO_ROOT)/run/$(PACKAGE).sock METRICS_ADDR=127.0.0.1:8080 \
		$(UV) python -m embedding_worker

