# Repo-level tasks. Per-service tasks live in services/<name>/Makefile; this delegates.

SERVICES := services/inferences-ocr
COMPOSE  := docker compose -f deployment/docker-compose.yml

.PHONY: help doctor test coverage build lock lock-check lock-upgrade go-work-sync

help:
	@echo "doctor        check this machine has the tools this repo needs"
	@echo "test          run every service's tests"
	@echo "coverage      run every service's coverage, reported per service"
	@echo "build         build every service image"
	@echo "lock          re-resolve uv.lock after editing a pyproject.toml"
	@echo "lock-check    fail if uv.lock is out of date (what the image build enforces)"
	@echo "lock-upgrade  re-resolve, allowing newer versions within the declared bounds"
	@echo "go-work-sync  add every module under packages/golibs to go.work"
	@echo ""
	@echo "services: $(SERVICES)"

doctor:
	@./scripts/doctor.sh

test:
	@for service in $(SERVICES); do \
		echo "== $$service"; \
		$(MAKE) --no-print-directory -C $$service test || exit $$?; \
	done

# Each service reports its own number against its own code. Never blended: a healthy
# service would otherwise mask an untested one.
coverage:
	@for service in $(SERVICES); do \
		echo "== $$service"; \
		$(MAKE) --no-print-directory -C $$service coverage || exit $$?; \
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
