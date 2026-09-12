# Repo-level tasks. Each unit owns its own Makefile; this delegates and never blends.
# A unit is any directory carrying a Makefile, and the list is discovered rather than
# written down, so one identical file is correct on every branch.

UNIT_ROOTS := $(wildcard services packages)

# maxdepth 4 reaches services/<svc>/examples/<lang>/, the deepest unit shape here.
UNIT_DIRS := $(if $(UNIT_ROOTS),$(shell find $(UNIT_ROOTS) -mindepth 2 -maxdepth 4 \
             -name Makefile ! -path '*/.*' 2>/dev/null | sed 's|/Makefile$$||' | sort))

GO_UNITS := $(strip $(foreach d,$(UNIT_DIRS),$(if $(wildcard $(d)/go.mod),$(d))))
PY_UNITS := $(filter-out $(GO_UNITS),$(UNIT_DIRS))
# strip matters: with both halves empty this is a single space, which $(if) reads as true.
UNITS    := $(strip $(PY_UNITS) $(GO_UNITS))

# Every go.mod on the branch, so modules without a test contract stay visible.
GO_MODULES   := $(if $(UNIT_ROOTS),$(shell find $(UNIT_ROOTS) -maxdepth 4 \
                -name go.mod ! -path '*/.*' 2>/dev/null | sed 's|/go\.mod$$||' | sort))
GO_UNTESTED  := $(filter-out $(GO_UNITS),$(GO_MODULES))

PY_PKGS := $(filter packages/pylibs/%,$(PY_UNITS))
GO_PKGS := $(filter packages/golibs/%,$(GO_UNITS))

# yaml is the worker's manifest reader; named here so it stays a decision rather than drift.
PY_ALLOW_worker := --allow yaml

# Which halves of the dependency gate this branch can actually run.
VERIFY_HALVES := $(strip $(if $(GO_UNITS),go-verify) $(if $(PY_PKGS),py-verify))
TEST_GATE := $(if $(VERIFY_HALVES),dip-verify)

# Keyed on discovered units, not directory existence: build output (.coverage, __pycache__)
# can leave a package directory on disk after checking out a branch that does not carry it.
GENERATE_HALVES := $(strip $(if $(filter packages/pylibs/dip,$(PY_PKGS)),py-generate) \
                           $(if $(filter packages/golibs/dip,$(GO_PKGS)),go-generate))

COMPOSE := docker compose -f deployment/docker-compose.yml
SCHEMA  := specs/dip/dip.schema.json

# Dev-only, pinned, never a runtime dependency of anything.
PY_CODEGEN := datamodel-code-generator==0.40.0
GO_CODEGEN := github.com/atombender/go-jsonschema@v0.20.0

# go-jsonschema restates every field name and repeats one UnmarshalJSON banner per type.
# Stripped during generation so a regeneration cannot bring it back; the IDL's own
# descriptions survive, because those say something the field name does not.
GO_GEN_NOISE := -e '/^[[:space:]]*\/\/ [A-Za-z0-9_]+ corresponds to the JSON schema field "[^"]*"\.$$/d' \
                -e '/^[[:space:]]*\/\/ UnmarshalJSON implements json\.Unmarshaler\.$$/d'

.PHONY: help units doctor test coverage build lock lock-check lock-upgrade \
        py-test py-coverage py-verify go-test go-coverage go-verify go-work-sync \
        dip-verify dip-generate dip-corpus py-generate go-generate

help:
	@echo "doctor        check this machine has the tools this repo needs"
	@echo "units         list the units discovered on this branch"
	@echo "test          run every discovered unit's tests"
	@echo "coverage      run every discovered unit's coverage, reported per unit"
	@echo "build         build every service image"
	@echo "lock          re-resolve uv.lock after editing a pyproject.toml"
	@echo "lock-check    fail if uv.lock is out of date (what the image build enforces)"
	@echo "lock-upgrade  re-resolve, allowing newer versions within the declared bounds"
	@echo "py-test       run the Python units' tests"
	@echo "py-coverage   run the Python units' coverage against their floors"
	@echo "py-verify     prove the Python packages have no unexpected dependencies"
	@echo "go-test       run the Go units' tests"
	@echo "go-coverage   run the Go units' coverage against their floors"
	@echo "go-verify     prove the Go modules have no third-party dependencies"
	@echo "go-work-sync  add every module under packages/golibs to go.work"
	@echo "dip-verify    run whichever halves of the dependency gate this branch has"
	@echo "dip-generate  regenerate the DIP types for the languages on this branch"
	@echo "dip-corpus    regenerate the DIP conformance corpus"
	@echo ""
	@$(MAKE) --no-print-directory units

units:
	@echo "python units: $(if $(PY_UNITS),$(PY_UNITS),(none))"
	@echo "go units:     $(if $(GO_UNITS),$(GO_UNITS),(none))"
	@echo "verify:       $(if $(VERIFY_HALVES),$(VERIFY_HALVES),(nothing to verify on this branch))"
	@$(if $(GO_UNTESTED),echo "go modules without a Makefile (no test contract -- not run): $(GO_UNTESTED)",:)

doctor:
	@./scripts/doctor.sh

test: $(TEST_GATE)
	@$(if $(UNITS),:,echo "no units on this branch -- nothing to test")
	@for unit in $(UNITS); do \
		echo "== $$unit"; \
		$(MAKE) --no-print-directory -C $$unit test || exit $$?; \
	done

# Never blended: a healthy unit would otherwise mask an untested one.
coverage:
	@$(if $(UNITS),:,echo "no units on this branch -- nothing to measure")
	@for unit in $(UNITS); do \
		echo "== $$unit"; \
		$(MAKE) --no-print-directory -C $$unit coverage || exit $$?; \
	done

py-test:
	@$(if $(PY_UNITS),:,echo "no Python units on this branch")
	@for unit in $(PY_UNITS); do \
		echo "== $$unit"; \
		$(MAKE) --no-print-directory -C $$unit test || exit $$?; \
	done

py-coverage:
	@$(if $(PY_UNITS),:,echo "no Python units on this branch")
	@for unit in $(PY_UNITS); do \
		echo "== $$unit"; \
		$(MAKE) --no-print-directory -C $$unit coverage || exit $$?; \
	done

go-test:
	@$(if $(GO_UNITS),:,echo "no Go units on this branch")
	@for unit in $(GO_UNITS); do \
		echo "== $$unit"; \
		$(MAKE) --no-print-directory -C $$unit test || exit $$?; \
	done

go-coverage:
	@$(if $(GO_UNITS),:,echo "no Go units on this branch")
	@for unit in $(GO_UNITS); do \
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
	@test -d packages/golibs || { echo "go-work-sync: packages/golibs is not on this branch"; exit 1; }
	go work use -r ./packages/golibs

# The generated code must not smuggle in a runtime library nobody chose.
dip-verify: $(VERIFY_HALVES)
	@$(if $(VERIFY_HALVES),:,echo "dip-verify: no packages on this branch -- nothing to verify")

# `.Standard` is go's own verdict, so std's vendored packages (vendor/golang.org/x/net,
# which arrives with `import "net"`) count as stdlib rather than third-party.
# go list's status is captured on its own line: `|| true` here would also swallow its
# failure and report "stdlib only" for a check that never ran.
define go_third_party
	deps=$$(go list -deps -f '{{if not .Standard}}{{.ImportPath}}{{end}}' ./... 2>&1) || { \
		echo "  CANNOT VERIFY: go list failed"; printf '%s\n' "$$deps" | sed 's/^/    /'; \
		exit 1; \
	}; \
	out=$$(printf '%s\n' "$$deps" | grep -v '^github.com/mrdhira/dita/' | grep -v '^$$' || true); \
	if [ -n "$$out" ]; then printf '%s\n' "$$out" | sed 's/^/  THIRD-PARTY: /'; exit 1; fi; \
	echo "  stdlib only"
endef

go-verify:
	@$(if $(GO_UNITS),:,echo "no Go modules to verify on this branch")
	@for unit in $(GO_UNITS); do \
		echo "== $$unit: go list -deps"; \
		(cd $$unit && $(go_third_party)) || exit $$?; \
	done

# foreach, not a shell loop: the allowance is per-package make data.
py-verify:
	@$(if $(PY_PKGS),:,echo "no Python packages to verify on this branch")
	@$(foreach unit,$(PY_PKGS), \
		echo "== $(unit): import scan"; \
		uv run --frozen python scripts/check_stdlib_only.py \
			$(unit)/src $(PY_ALLOW_$(notdir $(unit))) || exit $$?; \
	)

dip-generate: $(GENERATE_HALVES)
	@$(if $(GENERATE_HALVES),:,echo "dip-generate: no DIP package on this branch")

py-generate:
	@test -f $(SCHEMA) || { echo "$@: $(SCHEMA) is not on this branch"; exit 1; }
	uv run --frozen --with $(PY_CODEGEN) datamodel-codegen \
		--input $(SCHEMA) --input-file-type jsonschema \
		--output-model-type dataclasses.dataclass --target-python-version 3.14 \
		--disable-timestamp \
		--output packages/pylibs/dip/src/dip/types.py

go-generate:
	@test -f $(SCHEMA) || { echo "$@: $(SCHEMA) is not on this branch"; exit 1; }
	GOWORK=off GOFLAGS=-mod=mod go run $(GO_CODEGEN) \
		--package dip --tags json --output packages/golibs/dip/types.go $(SCHEMA)
	@tmp=$$(mktemp) && sed -E $(GO_GEN_NOISE) packages/golibs/dip/types.go > $$tmp \
		&& mv $$tmp packages/golibs/dip/types.go
	gofmt -w packages/golibs/dip/types.go

dip-corpus:
	@test -f specs/dip/conformance/build_corpus.py || { \
		echo "dip-corpus: specs/dip is not on this branch"; exit 1; }
	uv run --frozen python specs/dip/conformance/build_corpus.py
