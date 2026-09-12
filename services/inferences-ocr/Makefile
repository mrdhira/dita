PACKAGE   := inferences-ocr
REPO_ROOT := $(CURDIR)/../..
UV        := uv run --package $(PACKAGE) --locked

# A tripwire, not a target: set just below what the suite reaches.
COVERAGE_FLOOR := 87

.PHONY: help test coverage run image

help:
	@echo "test      run the test suite"
	@echo "coverage  run the suite under coverage and enforce the $(COVERAGE_FLOOR)% floor"
	@echo "run       start the worker against ./models and ../../run"
	@echo "image     build the image (context is the repo root, where uv.lock lives)"

test:
	$(UV) python -m unittest discover -s tests -t .

coverage:
	$(UV) coverage run -m unittest discover -s tests -t .
	$(UV) coverage report --fail-under=$(COVERAGE_FLOOR)

run:
	MODELS_DIR=./models SOCKET_PATH=$(REPO_ROOT)/run/$(PACKAGE).sock \
		$(UV) python -m ocr_worker

image:
	docker build -f $(CURDIR)/Dockerfile -t dita-$(PACKAGE):dev $(REPO_ROOT)
