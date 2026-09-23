PACKAGE      := dita-orchestrator
COVERAGE_OUT := coverage.out

# `go test -cover` exits zero at any percentage, so the floor is enforced below. A tripwire,
# not a target: set just under what the suite reaches. The chat handler, cmd and main have
# no tests, which is most of what keeps it this low.
COVERAGE_FLOOR := 54

.PHONY: help test coverage build image up down

help:
	@echo "test      run the suite under the race detector"
	@echo "coverage  run it and enforce the $(COVERAGE_FLOOR)% floor"
	@echo "build     compile the static binary, as the image does"
	@echo "image     build the image"
	@echo "up        start the gateway stack (compose.yaml, on the proxy network)"
	@echo "down      stop it"

test:
	go test -race ./...

coverage:
	go test -coverprofile=$(COVERAGE_OUT) ./...
	@go tool cover -func=$(COVERAGE_OUT) | awk -v floor=$(COVERAGE_FLOOR) ' \
		/^total:/ { total = $$3; sub("%", "", total); seen = 1 } \
		END { \
			if (!seen) { print "no total line in the coverage report: the floor never ran"; exit 1 } \
			if (total + 0 < floor + 0) { printf "coverage %s%% is under the %s%% floor\n", total, floor; exit 1 } \
			printf "coverage %s%% clears the %s%% floor\n", total, floor \
		}'

build:
	CGO_ENABLED=0 go build -ldflags="-s -w" -o bin/$(PACKAGE) .

image:
	docker build -t $(PACKAGE):local .

up:
	docker compose -f $(CURDIR)/compose.yaml up -d --build

down:
	docker compose -f $(CURDIR)/compose.yaml down
