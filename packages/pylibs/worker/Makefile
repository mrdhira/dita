# Tasks for this package only. Run from here, or via the root Makefile.

PACKAGE := worker
UV      := uv run --package $(PACKAGE) --locked

# A tripwire, not a target: a test that asserts nothing raises coverage just as well as a
# test that asserts something. Set just below what the suite reaches.
COVERAGE_FLOOR := 92

.PHONY: help test coverage

help:
	@echo "test      run the suite, including the worker skeleton over a real socket"
	@echo "coverage  run the suite under coverage and enforce the $(COVERAGE_FLOOR)% floor"

test:
	$(UV) python -m unittest discover -s tests -t .

coverage:
	$(UV) coverage run -m unittest discover -s tests -t .
	$(UV) coverage report --fail-under=$(COVERAGE_FLOOR)
