# Tasks for this package only. Run from here, or via the root Makefile.

PACKAGE := dip

# A tripwire, not a target: a test that asserts nothing raises coverage just as well as a
# test that asserts something. Set just below what the suite reaches.
#
# `go test -cover` prints a percentage and exits zero at any percentage, so unlike the
# Python units there is no --fail-under to lean on and the floor is checked here.
COVERAGE_FLOOR := 78
COVERAGE_OUT   := coverage.out

# The conformance corpus is specs/dip/conformance at the repository root. It belongs to
# neither implementation, so it lives outside this module and a checkout of this module
# alone may not have it; the corpus cases skip when it is missing.
#
# That skip is reported here because `go test` will not report it: without -v the tool
# prints nothing for a package whose tests skip -- not even output written straight to
# stderr, which it buffers and discards for a package that passes. The coverage floor is no
# help either, since the package's own tests clear it with or without the corpus. So the
# status is printed before every run, where it is impossible to miss.
CORPUS_DIR := $(or $(DIP_CONFORMANCE_CORPUS),$(CURDIR)/../../../specs/dip/conformance)

.PHONY: help test coverage corpus

help:
	@echo "test      run the suite, including the conformance corpus"
	@echo "coverage  run the suite and enforce the $(COVERAGE_FLOOR)% floor for $(PACKAGE)"
	@echo "corpus    report whether the shared conformance corpus is present"

corpus:
	@if [ -d "$(CORPUS_DIR)" ]; then \
		echo "conformance corpus: present at $(CORPUS_DIR)"; \
	else \
		echo "conformance corpus: ABSENT at $(CORPUS_DIR)"; \
		echo "                    the corpus cases will SKIP, and go test will not say so."; \
		echo "                    Set DIP_CONFORMANCE_CORPUS to a corpus directory to run them."; \
	fi

test: corpus
	go test ./...

coverage: corpus
	go test -coverprofile=$(COVERAGE_OUT) ./...
	@go tool cover -func=$(COVERAGE_OUT) | awk -v floor=$(COVERAGE_FLOOR) ' \
		/^total:/ { total = $$3; sub("%", "", total); seen = 1 } \
		END { \
			if (!seen) { print "no total line in the coverage report: the floor never ran"; exit 1 } \
			if (total + 0 < floor + 0) { printf "coverage %s%% is under the %s%% floor\n", total, floor; exit 1 } \
			printf "coverage %s%% clears the %s%% floor\n", total, floor \
		}'
