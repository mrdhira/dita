# Tasks for this package only. Run from here, or via the root Makefile.

PACKAGE := dip

# A tripwire, not a target: a test that asserts nothing raises coverage just as well as a
# test that asserts something. Set just below what the suite reaches.
#
# `go test -cover` prints a percentage and exits zero at any percentage, so unlike the
# Python units there is no --fail-under to lean on and the floor is checked here.
COVERAGE_FLOOR := 67
COVERAGE_OUT   := coverage.out

.PHONY: help test coverage

help:
	@echo "test      run the suite, including the conformance corpus"
	@echo "coverage  run the suite and enforce the $(COVERAGE_FLOOR)% floor for $(PACKAGE)"

test:
	go test ./...

coverage:
	go test -coverprofile=$(COVERAGE_OUT) ./...
	@go tool cover -func=$(COVERAGE_OUT) | awk -v floor=$(COVERAGE_FLOOR) ' \
		/^total:/ { total = $$3; sub("%", "", total); seen = 1 } \
		END { \
			if (!seen) { print "no total line in the coverage report: the floor never ran"; exit 1 } \
			if (total + 0 < floor + 0) { printf "coverage %s%% is under the %s%% floor\n", total, floor; exit 1 } \
			printf "coverage %s%% clears the %s%% floor\n", total, floor \
		}'
