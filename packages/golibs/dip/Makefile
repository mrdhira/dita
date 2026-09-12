# Tasks for this package only. Run from here, or via the root Makefile.

PACKAGE := dip

.PHONY: help test coverage

help:
	@echo "test      run the suite, including the conformance corpus"
	@echo "coverage  run the suite and report coverage for $(PACKAGE)"

test:
	go test ./...

coverage:
	go test -cover ./...
