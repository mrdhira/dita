# ONNXRuntime-only OCR worker. Build context is the REPO ROOT: uv.lock lives there and a
# context cannot reach outside itself.
# Exact patch and OS suffix: a rolling tag changes what ships, and the apt names below
# are Debian-release specific.
FROM ghcr.io/astral-sh/uv:0.12.13 AS uv

FROM python:3.14.6-slim-trixie AS deps

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /workspace

# Manifests and lock only, so editing the worker does not rebuild dependencies.
COPY pyproject.toml uv.lock ./
COPY services/inferences-ocr/pyproject.toml services/inferences-ocr/pyproject.toml
# Workspace members this service depends on: --locked reads every manifest in the lock.
COPY packages/pylibs/dip/pyproject.toml packages/pylibs/dip/pyproject.toml
COPY packages/pylibs/dita-worker/pyproject.toml packages/pylibs/dita-worker/pyproject.toml

# --locked, not --frozen: only --locked verifies the lock against the manifests, so a
# bound edited without `make lock` fails the build here.
# --no-default-groups keeps dev-only packages out of the runtime image.
# --no-emit-package dip/dita-worker: first-party source, copied in below like ocr_worker
# rather than pip-installed -- an editable entry carries no hash and would fail
# --require-hashes. Their own third-party dependencies stay in the export.
RUN uv export --package inferences-ocr --locked --no-emit-project \
        --no-emit-package dip --no-emit-package dita-worker \
        --no-default-groups --no-cache -o /tmp/requirements.txt

FROM python:3.14.6-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODELS_DIR=/models \
    SOCKET_PATH=/run/dita/inferences-ocr.sock

# libgl*/libglib*: opencv links against them. tesseract-ocr*: that engine is a binary.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0t64 \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-jpn \
        tesseract-ocr-jpn-vert \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin ocr

WORKDIR /app

# --require-hashes: refuse a wheel whose bytes differ, or a file missing any hash.
COPY --from=deps /tmp/requirements.txt ./requirements.lock.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.lock.txt \
    && rm requirements.lock.txt

COPY services/inferences-ocr/models.yaml ./
COPY services/inferences-ocr/ocr_worker ./ocr_worker
COPY packages/pylibs/dip/src/dip ./dip
COPY packages/pylibs/dita-worker/src/dita_worker ./dita_worker

# Owned by `ocr` so the non-root user can write when run without bind mounts.
RUN mkdir -p "$MODELS_DIR" "$(dirname "$SOCKET_PATH")" \
    && chown -R ocr:ocr "$MODELS_DIR" "$(dirname "$SOCKET_PATH")"

USER ocr

# An exec probe: this service has no HTTP surface.
HEALTHCHECK --start-period=30s --interval=30s --timeout=10s --retries=3 \
    CMD ["python", "-m", "ocr_worker", "--probe", "ready"]

ENTRYPOINT ["python", "-m", "ocr_worker"]
