# ONNXRuntime-only OCR worker. No torch, no paddle, no CUDA.
#
# Python 3.14 to match .tool-versions. The OS suffix is pinned deliberately: the apt
# package names below are Debian-release specific (trixie renamed libglib2.0-0 to
# libglib2.0-0t64), so an unpinned `python:3.14-slim` would break on the next Debian bump.
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

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY models.yaml ./
COPY ocr_worker ./ocr_worker

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
