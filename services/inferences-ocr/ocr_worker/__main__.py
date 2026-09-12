"""Entry point: python -m ocr_worker

Everything this file does not say is `dita_worker`'s: the flags, the socket server, the
one-model-resident manager, the fetcher and the health probes. What is OCR is the engine
factory and the manifest beside it.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dita_worker import Worker, cli

from . import __version__
from .engines import ENGINE_NAMES, build_engine

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "models.yaml"

WORKER = Worker(
    name="inferences-ocr",
    version=__version__,
    engines=ENGINE_NAMES,
    build_engine=build_engine,
    registry_path=REGISTRY_PATH,
    prog="ocr_worker",
    log_level_env="OCR_LOG_LEVEL",
)


def main(argv: list[str] | None = None) -> int:
    return cli.main(WORKER, argv)


if __name__ == "__main__":
    sys.exit(main())
