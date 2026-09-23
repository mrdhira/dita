PACKAGE := dip

# `go test -cover` exits zero at any percentage, so there is no --fail-under to lean on and
# the floor is enforced below. A tripwire, not a target: set just under what the suite reaches.
COVERAGE_FLOOR := 78
COVERAGE_OUT   := coverage.out

# `go test` without -v prints nothing for a package whose tests skip, and the floor clears
# with or without the corpus, so an absent corpus would silently stop running its cases.
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
