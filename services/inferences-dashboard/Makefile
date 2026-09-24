PACKAGE := inferences-dashboard
# --frozen-lockfile: the lock is committed and exact; an install that would change it fails.
PNPM    := pnpm

.PHONY: help install test coverage lint build e2e audit up down

help:
	@echo "install   install exactly what pnpm-lock.yaml pins"
	@echo "test      lint, then the unit suite (Vitest + React Testing Library)"
	@echo "coverage  the unit suite under coverage, with the floors in vite.config.ts"
	@echo "lint      eslint and prettier --check"
	@echo "build     typecheck, bundle, fail on any key-shaped string in dist/, write the SBOM"
	@echo "e2e       the Playwright scenario against a real orchestrator and a stub worker"
	@echo "audit     pnpm audit --prod"
	@echo "up        serve dist/ through Caddy on loopback (compose.yaml)"
	@echo "down      stop it"

install:
	$(PNPM) install --frozen-lockfile

test: install
	$(PNPM) lint
	$(PNPM) test

coverage: install
	$(PNPM) coverage

lint: install
	$(PNPM) lint

build: install
	$(PNPM) build

# The real orchestrator binary over a fresh store, Caddy with the production headers, and
# the labelled stub in place of inferences-system-one. Headless, one worker: this box is tight.
e2e: install build
	$(MAKE) -C ../dita-orchestrator build
	$(PNPM) e2e

audit:
	$(PNPM) audit --prod

up: build
	docker compose -f $(CURDIR)/compose.yaml up -d

down:
	docker compose -f $(CURDIR)/compose.yaml down
