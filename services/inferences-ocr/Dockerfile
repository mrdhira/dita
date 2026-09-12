# ONNXRuntime-only OCR worker. No torch, no paddle, no CUDA.
#
# The build context is the REPO ROOT, not this directory (see deployment/docker-compose.yml).
# It has to be: the workspace root pyproject.toml and the single uv.lock live at the top of
# the repo, and a build context cannot reach outside itself.
#
# Python 3.14 to match .tool-versions. The OS suffix is pinned deliberately: the apt package
# names below are Debian-release specific (trixie renamed libglib2.0-0 to libglib2.0-0t64),
# so an unpinned `python:3.14-slim` would break on the next Debian bump.

# Pinned, never :latest -- this is the tool that decides which bytes get installed.
FROM ghcr.io/astral-sh/uv:0.12.13 AS uv

# Stage 1: turn the workspace lock into a fully pinned, hash-carrying requirements file for
# this one member. uv stays in this stage; the runtime image never sees it.
FROM python:3.14-slim-trixie AS deps

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /workspace

# Only the manifests and the lock. Nothing here depends on the service source, so editing
# the worker does not invalidate the dependency layer.
COPY pyproject.toml uv.lock ./
COPY services/inferences-ocr/pyproject.toml services/inferences-ocr/pyproject.toml

# --locked, not --frozen. Both refuse to update the lock, but only --locked *verifies* it
# against the manifests: `uv export --frozen` will happily export a stale lock and exit 0.
# This line is the guard that replaces the old weekly deps-check workflow -- edit a bound in
# a pyproject.toml without running `make lock` and the image build fails here.
# --no-default-groups keeps the dev group (coverage) out of the runtime image. Without
# it uv exports the default groups too, and a test-only package would ship to production.
RUN uv export --package inferences-ocr --locked --no-emit-project --no-default-groups \
        --no-cache -o /tmp/requirements.txt

FROM python:3.14-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODELS_DIR=/models \
    SOCKET_PATH=/run/dita/inferences-ocr.sock

# libgl1/libglib2.0-0t64: shared libraries opencv (a rapidocr dependency) links against.
# tesseract-ocr*: the `tesseract` registry entry is a system binary, not a download.
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

# Every version and every hash comes from uv.lock. --require-hashes makes pip refuse a
# wheel whose bytes do not match, and refuse the file at all if any entry lacks a hash.
COPY --from=deps /tmp/requirements.txt ./requirements.lock.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.lock.txt \
    && rm requirements.lock.txt

COPY services/inferences-ocr/models.yaml ./
COPY services/inferences-ocr/ocr_worker ./ocr_worker

# Both are bind mounts in deployment/docker-compose.yml. Creating them owned by `ocr`
# keeps the non-root user able to write when the image is run without explicit mounts.
RUN mkdir -p "$MODELS_DIR" "$(dirname "$SOCKET_PATH")" \
    && chown -R ocr:ocr "$MODELS_DIR" "$(dirname "$SOCKET_PATH")"

USER ocr

# The image carries its own probe, so `docker run` without compose still reports health.
# An exec probe because there is no HTTP surface here and none is being added for this.
HEALTHCHECK --start-period=30s --interval=30s --timeout=10s --retries=3 \
    CMD ["python", "-m", "ocr_worker", "--probe", "ready"]

# Weights are fetched on the first `load`, never baked into the image.
ENTRYPOINT ["python", "-m", "ocr_worker"]
