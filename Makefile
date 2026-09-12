# Repo-level tasks. Each unit owns its own Makefile; this delegates and never blends.
#
# A unit is anything with its own tests and its own coverage number: a service, a shared
# package, or an example. Examples are units too -- they are code someone will copy.

PY_UNITS := services/inferences-ocr packages/pylibs/dip packages/pylibs/dita-worker
GO_UNITS := packages/golibs/dip services/inferences-ocr/examples/go
UNITS    := $(PY_UNITS) $(GO_UNITS)

COMPOSE  := docker compose -f deployment/docker-compose.yml
SCHEMA   := specs/dip/dip.schema.json

# Dev-only, pinned, never a runtime dependency of anything.
PY_CODEGEN := datamodel-code-generator==0.40.0
GO_CODEGEN := github.com/atombender/go-jsonschema@v0.20.0

.PHONY: help doctor test coverage build lock lock-check lock-upgrade go-work-sync \
        dip-generate dip-corpus dip-verify

help:
	@echo "doctor        check this machine has the tools this repo needs"
	@echo "test          run every unit's tests, both languages"
	@echo "coverage      run every unit's coverage, reported per unit"
	@echo "build         build every service image"
	@echo "lock          re-resolve uv.lock after editing a pyproject.toml"
	@echo "lock-check    fail if uv.lock is out of date (what the image build enforces)"
	@echo "lock-upgrade  re-resolve, allowing newer versions within the declared bounds"
	@echo "go-work-sync  add every module under packages/golibs to go.work"
	@echo "dip-generate  regenerate the DIP types for both languages from the IDL"
	@echo "dip-corpus    regenerate the DIP conformance corpus"
	@echo "dip-verify    prove the generated DIP code has no third-party dependencies"
	@echo ""
	@echo "units: $(UNITS)"

doctor:
	@./scripts/doctor.sh

test:
	@for unit in $(UNITS); do \
		echo "== $$unit"; \
		$(MAKE) --no-print-directory -C $$unit test || exit $$?; \
	done

# Each unit reports its own number against its own code. Never blended: a healthy unit
# would otherwise mask an untested one.
coverage:
	@for unit in $(UNITS); do \
		echo "== $$unit"; \
		$(MAKE) --no-print-directory -C $$unit coverage || exit $$?; \
	done

build:
	$(COMPOSE) build

lock:
	uv lock

lock-check:
	uv lock --check

lock-upgrade:
	uv lock --upgrade

# go.work takes no glob, so new golibs modules are expanded into it explicitly.
go-work-sync:
	go work use -r ./packages/golibs

dip-generate:
	uv run --frozen --with $(PY_CODEGEN) datamodel-codegen \
		--input $(SCHEMA) --input-file-type jsonschema \
		--output-model-type dataclasses.dataclass --target-python-version 3.14 \
		--disable-timestamp \
		--output packages/pylibs/dip/src/dip/types.py
	GOWORK=off GOFLAGS=-mod=mod go run $(GO_CODEGEN) \
		--package dip --tags json --output packages/golibs/dip/types.go $(SCHEMA)
	gofmt -w packages/golibs/dip/types.go

dip-corpus:
	uv run --frozen python specs/dip/conformance/build_corpus.py

# The acceptance criterion for the generated code: a runtime library smuggled in by a
# generator would be a dependency nobody chose.
#
# `.Standard` is go's own verdict, so std's vendored packages (vendor/golang.org/x/net,
# which arrives with `import "net"`) count as stdlib. Matching on a dotted path instead
# would flag them, and flag the module's own packages, which `go list -deps` always lists.
GO_THIRD_PARTY = go list -deps -f '{{if not .Standard}}{{.ImportPath}}{{end}}' ./... 	| grep -v '^github.com/mrdhira/dita/' || true

dip-verify:
	@echo "== packages/golibs/dip: go list -deps"
	@cd packages/golibs/dip && out=$$($(GO_THIRD_PARTY)); \
		if [ -n "$$out" ]; then echo "$$out" | sed 's/^/  THIRD-PARTY: /'; exit 1; fi; \
		echo "  stdlib only"
	@echo "== services/inferences-ocr/examples/go: go list -deps"
	@cd services/inferences-ocr/examples/go && out=$$($(GO_THIRD_PARTY)); \
		if [ -n "$$out" ]; then echo "$$out" | sed 's/^/  THIRD-PARTY: /'; exit 1; fi; \
		echo "  stdlib only"
	@echo "== packages/pylibs/dip: import scan"
	@uv run --frozen python scripts/check_stdlib_only.py packages/pylibs/dip/src
	@echo "== packages/pylibs/dita-worker: import scan"
	@# yaml is the manifest reader's one dependency, declared in the package manifest and
	@# named here so it stays a decision rather than a drift.
	@uv run --frozen python scripts/check_stdlib_only.py \
		packages/pylibs/dita-worker/src --allow yaml
