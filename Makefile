# dita — repo-level tasks. Per-service detail lives in each service's README.

OCR_DIR   := services/inferences-ocr
OCR_VENV  := $(OCR_DIR)/.venv
PYTHON    := 3.14
COMPOSE   := docker compose -f deployment/docker-compose.yml

.PHONY: help build test deps-check

help:
	@echo "build       build every service image"
	@echo "test        run the inferences-ocr test suite"
	@echo "deps-check  report outdated Python dependencies for inferences-ocr"

build:
	$(COMPOSE) build

test:
	cd $(OCR_DIR) && python -m unittest discover -s tests -t .

# Uses uv rather than pip: it resolves against the index without installing, so the check
# is fast and cannot mutate the venv it is inspecting.
deps-check:
	@command -v uv >/dev/null 2>&1 || { \
		echo "uv is not installed. See https://docs.astral.sh/uv/getting-started/installation/"; \
		exit 127; \
	}
	@test -d $(OCR_VENV) || uv venv --python $(PYTHON) $(OCR_VENV)
	@VIRTUAL_ENV=$(OCR_VENV) uv pip install --quiet -r $(OCR_DIR)/requirements.txt
	@echo "== $(OCR_DIR) =="
	@VIRTUAL_ENV=$(OCR_VENV) uv pip list --outdated
