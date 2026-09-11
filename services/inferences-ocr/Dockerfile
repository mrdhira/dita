# ONNXRuntime-only OCR worker. No torch, no paddle, no CUDA.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODELS_DIR=/models \
    SOCKET_PATH=/run/dita/inferences-ocr.sock

# libgl1/libglib2.0-0: shared libraries opencv (a rapidocr dependency) links against.
# tesseract-ocr*: the `tesseract` registry entry is a system binary, not a download.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
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

# Both of these are mount points. Creating them owned by `ocr` in the image matters:
# a docker named volume inherits the ownership of the directory it first covers, which
# is what lets the non-root user write to them.
RUN mkdir -p "$MODELS_DIR" "$(dirname "$SOCKET_PATH")" \
    && chown -R ocr:ocr "$MODELS_DIR" "$(dirname "$SOCKET_PATH")"

VOLUME ["/models", "/run/dita"]

USER ocr

# Weights are fetched on the first `load`, never baked into the image.
ENTRYPOINT ["python", "-m", "ocr_worker"]
