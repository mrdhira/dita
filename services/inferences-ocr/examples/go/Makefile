# The example is its own unit: it is code someone will copy, so it carries its own tests
# and its own coverage number rather than hiding inside the service it demonstrates.

.PHONY: help test coverage run

help:
	@echo "test      run the example's tests"
	@echo "coverage  run them with coverage"
	@echo "run       drive a live worker (SOCKET=... IMAGE=...)"

test:
	go test ./...

coverage:
	go test -cover ./...

SOCKET ?= $(CURDIR)/../../../../run/inferences-ocr.sock
IMAGE  ?=

run:
	go run . -socket $(SOCKET) -image $(IMAGE)
