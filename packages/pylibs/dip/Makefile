PACKAGE := dip
UV      := uv run --package $(PACKAGE) --locked

# A tripwire, not a target: set just below what the suite reaches.
COVERAGE_FLOOR := 97

.PHONY: help test coverage

help:
	@echo "test      run the suite, including the conformance corpus in specs/dip"
	@echo "coverage  run the suite under coverage and enforce the $(COVERAGE_FLOOR)% floor"

test:
	$(UV) python -m unittest discover -s tests -t .

coverage:
	$(UV) coverage run -m unittest discover -s tests -t .
	$(UV) coverage report --fail-under=$(COVERAGE_FLOOR)
