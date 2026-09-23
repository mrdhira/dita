# ONNXRuntime-only cross-encoder reranker. Build context is the REPO ROOT: uv.lock lives there
# and a context cannot reach outside itself.
FROM ghcr.io/astral-sh/uv:0.12.17 AS uv

FROM python:3.14.6-slim-trixie AS deps

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /workspace

# Manifests and lock only, so editing the worker does not rebuild dependencies. Every
# member's manifest, not only this one's: --locked compares the lock with the whole workspace.
COPY pyproject.toml uv.lock ./
COPY --parents services/*/pyproject.toml packages/pylibs/*/pyproject.toml ./

# --locked, not --frozen: only --locked verifies the lock against the manifests. The workspace
# packages are copied in as source below; an editable entry carries no hash for pip to check.
RUN uv export --package inferences-reranker --locked --no-emit-project \
        --no-emit-package dip --no-emit-package worker --no-emit-package textinfer \
        --no-default-groups --no-cache -o /tmp/requirements.txt

FROM python:3.14.6-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODELS_DIR=/models \
    SOCKET_PATH=/run/dita/inferences-reranker.sock

RUN useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin reranker

WORKDIR /app

COPY --from=deps /tmp/requirements.txt ./requirements.lock.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.lock.txt \
    && rm requirements.lock.txt

COPY services/inferences-reranker/models.yaml ./
COPY services/inferences-reranker/reranker_worker ./reranker_worker
COPY packages/pylibs/dip/src/dip ./dip
COPY packages/pylibs/worker/src/worker ./worker
COPY packages/pylibs/textinfer/src/textinfer ./textinfer

RUN mkdir -p "$MODELS_DIR" "$(dirname "$SOCKET_PATH")" \
    && chown -R reranker:reranker "$MODELS_DIR" "$(dirname "$SOCKET_PATH")"

USER reranker

HEALTHCHECK --start-period=120s --interval=30s --timeout=10s --retries=3 \
    CMD ["python", "-m", "reranker_worker", "--probe", "ready"]

ENTRYPOINT ["python", "-m", "reranker_worker"]
