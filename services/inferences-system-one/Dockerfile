# ONNXRuntime-only System One worker. Build context is the REPO ROOT: uv.lock lives there and a
# context cannot reach outside itself.
#
# Four stages. `export` is the only one with torch: it fetches the pinned checkpoint and exports
# the encoder to ONNX. `parity` proves that graph against the torch reference without torch. The
# runtime copies the graph from `parity`, so an image cannot exist unless the guard passed.
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
RUN uv export --package inferences-system-one --locked --no-emit-project \
        --no-emit-package dip --no-emit-package worker --no-emit-package textinfer \
        --no-default-groups --no-cache -o /tmp/requirements.txt

# `uv sync` rather than an exported list: the lock is what knows torch comes from the CPU index.
FROM deps AS export

ENV UV_PROJECT_ENVIRONMENT=/opt/export-venv \
    PYTHONPATH=/workspace/src

RUN uv sync --package inferences-system-one --locked --group export --no-default-groups \
        --no-install-workspace --no-cache

COPY packages/pylibs/dip/src/dip ./src/dip
COPY packages/pylibs/worker/src/worker ./src/worker
COPY services/inferences-system-one/models.yaml services/inferences-system-one/export/export_encoder.py ./

RUN /opt/export-venv/bin/python export_encoder.py \
        --registry models.yaml --models-dir /models --out /models/onnx

FROM python:3.14.6-slim-trixie AS runtime-deps

COPY --from=deps /tmp/requirements.txt ./requirements.lock.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.lock.txt \
    && rm requirements.lock.txt

FROM runtime-deps AS parity

WORKDIR /app
COPY packages/pylibs/dip/src/dip ./dip
COPY packages/pylibs/worker/src/worker ./worker
COPY packages/pylibs/textinfer/src/textinfer ./textinfer
COPY services/inferences-system-one/models.yaml ./
COPY services/inferences-system-one/system_one_worker ./system_one_worker
COPY services/inferences-system-one/tests ./tests
COPY --from=export /models /models

# A skipped guard exits 0, so a skip fails the build as surely as a mismatch does.
RUN SYSTEM_ONE_PARITY_MODELS=/models SYSTEM_ONE_ONNX_DIR=/models/onnx \
        python -m unittest -v tests.test_parity > /tmp/parity.log 2>&1; status=$?; \
    cat /tmp/parity.log; [ "$status" -eq 0 ] && ! grep -q skipped /tmp/parity.log

FROM runtime-deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODELS_DIR=/models \
    SOCKET_PATH=/run/dita/inferences-system-one.sock \
    METRICS_ADDR=0.0.0.0:8080 \
    SYSTEM_ONE_ONNX_DIR=/opt/system-one/encoder

RUN useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin systemone

WORKDIR /app

COPY services/inferences-system-one/models.yaml ./
COPY services/inferences-system-one/system_one_worker ./system_one_worker
COPY packages/pylibs/dip/src/dip ./dip
COPY packages/pylibs/worker/src/worker ./worker
COPY packages/pylibs/textinfer/src/textinfer ./textinfer
# The graph only, outside /app: a directory named onnx on sys.path would shadow the package.
# The weights it came from are fetched into $MODELS_DIR at load, like every worker's.
COPY --from=parity /models/onnx /opt/system-one/encoder

RUN mkdir -p "$MODELS_DIR" "$(dirname "$SOCKET_PATH")" \
    && chown -R systemone:systemone "$MODELS_DIR" "$(dirname "$SOCKET_PATH")"

USER systemone

# HTTP /health, not the DIP readyz: /health is 503 until a model is resident, so a failed preload
# is unhealthy instead of a green container answering 503 forever.
HEALTHCHECK --start-period=300s --interval=30s --timeout=10s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request as u; sys.exit(u.urlopen('http://127.0.0.1:8080/health', timeout=5).status != 200)"]

ENTRYPOINT ["python", "-m", "system_one_worker"]
