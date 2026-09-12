# The example is its own unit: it is code someone will copy, so it carries its own tests
# and its own coverage number rather than hiding inside the service it demonstrates.

# A tripwire, not a target, and set just below what the suite reaches. It is low because
# most of this file is `run`, which only a live worker exercises; it exists so the helpers
# below it cannot quietly lose their tests.
#
# `go test -cover` prints a percentage and exits zero at any percentage, so unlike the
# Python units there is no --fail-under to lean on and the floor is checked here.
COVERAGE_FLOOR := 14
COVERAGE_OUT   := coverage.out

.PHONY: help test coverage run

help:
	@echo "test      run the example's tests"
	@echo "coverage  run them and enforce the $(COVERAGE_FLOOR)% floor"
	@echo "run       drive a live worker (SOCKET=... IMAGE=...)"

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

SOCKET ?= $(CURDIR)/../../../../run/inferences-ocr.sock
IMAGE  ?=

run:
	go run . -socket $(SOCKET) -image $(IMAGE)
