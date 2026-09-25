PACKAGE   := inferences-system-one
REPO_ROOT := $(CURDIR)/../..
UV        := uv run --package $(PACKAGE) --locked

# A tripwire, not a target: set just below what the suite reaches.
COVERAGE_FLOOR := 98

# Where `make export` puts the fetched inputs and the encoder graph, and `make parity` reads them.
MODELS := $(CURDIR)/models
ONNX   := $(MODELS)/laya-multilingual/onnx
# torch never enters the workspace venv the tests run in: the export gets an env of its own,
# inside the gitignored models directory.
EXPORT_ENV := $(MODELS)/.export-venv

.PHONY: help test coverage export golden reference parity run image up down

help:
	@echo "test      run the test suite (the parity guard skips: no weights)"
	@echo "coverage  run the suite under coverage and enforce the $(COVERAGE_FLOOR)% floor"
	@echo "export    fetch the pinned inputs into ./models and export the encoder (torch, via uv --group export)"
	@echo "golden    regenerate tests/fixtures/golden_head.npz: upstream's head in torch, fake-size weights"
	@echo "reference regenerate tests/fixtures/ref_out.json: upstream's reference code on the real weights"
	@echo "parity    run the parity guard against ./models, torch-free"
	@echo "run       start the worker against ./models and ../../run, HTTP on 127.0.0.1:8082"
	@echo "image     build the image (context is the repo root, where uv.lock lives)"
	@echo "up        start the deployed stack (compose.yaml, on the proxy network)"
	@echo "down      stop it"

test:
	$(UV) python -m unittest discover -s tests -t .

coverage:
	$(UV) coverage run -m unittest discover -s tests -t .
	$(UV) coverage report --fail-under=$(COVERAGE_FLOOR)

export:
	UV_PROJECT_ENVIRONMENT=$(EXPORT_ENV) uv run --package $(PACKAGE) --locked --group export \
		python export/export_encoder.py \
		--registry models.yaml --models-dir $(MODELS) --out $(ONNX)

golden:
	UV_PROJECT_ENVIRONMENT=$(EXPORT_ENV) uv run --package $(PACKAGE) --locked --group export \
		python export/golden_head.py

reference:
	UV_PROJECT_ENVIRONMENT=$(EXPORT_ENV) uv run --package $(PACKAGE) --locked --group export \
		python export/reference.py --models-dir $(MODELS)

parity:
	SYSTEM_ONE_PARITY_MODELS=$(MODELS) $(UV) python -m unittest -v tests.test_parity

run:
	MODELS_DIR=$(MODELS) SOCKET_PATH=$(REPO_ROOT)/run/$(PACKAGE).sock METRICS_ADDR=127.0.0.1:8082 \
		$(UV) python -m system_one_worker

image:
	docker build -f $(CURDIR)/Dockerfile -t dita-$(PACKAGE):dev $(REPO_ROOT)

up:
	docker compose -f $(CURDIR)/compose.yaml up -d --build

down:
	docker compose -f $(CURDIR)/compose.yaml down
