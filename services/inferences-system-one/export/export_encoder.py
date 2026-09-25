"""Export Laya's encoder to ONNX. Runs only in the image's builder stage: the one place torch exists.

Fetches the pinned inputs through the worker's own fetcher (so the sha256 in models.yaml gates
them here as at runtime), rebuilds the encoder exactly as the reference does
(`AutoModel.from_config` over `encoder/config.json`, the checkpoint's `encoder.*` tensors,
strict), exports it with the dynamo exporter at opset 18 in fp32, then refuses to finish unless
onnxruntime reproduces torch's hidden states on inputs that pad and cross the sliding window.
That check is a gross-error trip; precision is the parity stage's, on real inputs and answers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import onnxruntime as ort
import torch
import transformers
from safetensors.torch import load_file
from transformers import AutoConfig, AutoModel
from worker import ensure_model, load_registry, model_dir

OPSET = 18
# Relative to the hidden states' RMS (about 1.75). Measured 4.9e-4 of it at 2x1024; a wrong mask
# or layer misses by the order of the RMS itself. An absolute 1e-3 sat at 86% on float noise.
MAX_HIDDEN_DIFF_OF_RMS = 1e-2


class Encoder(torch.nn.Module):
    def __init__(self, encoder: torch.nn.Module) -> None:
        super().__init__()
        self.encoder = encoder

    def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        return self.encoder(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state


def build(directory: Path, weights: str) -> torch.nn.Module:
    encoder = AutoModel.from_config(AutoConfig.from_pretrained(directory / "encoder"), attn_implementation="sdpa")
    state = {k.removeprefix("encoder."): v for k, v in load_file(directory / weights).items()
             if k.startswith("encoder.")}
    encoder.load_state_dict(state, strict=True)
    encoder.config.reference_compile = False
    return Encoder(encoder).eval()


def sample(vocab: int, rows: int, length: int, padded_from: int) -> tuple[torch.Tensor, torch.Tensor]:
    generator = torch.Generator().manual_seed(length)
    ids = torch.randint(5, vocab, (rows, length), generator=generator)
    mask = torch.ones_like(ids)
    mask[-1, padded_from:] = 0
    return ids, mask


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    registry = load_registry(args.registry)
    spec = registry.get(registry.default_model)
    ensure_model(args.models_dir, spec)
    directory = model_dir(args.models_dir, spec)
    options = spec.options
    pinned = next(f for f in spec.files if f.dest == options["weights"])

    model = build(directory, options["weights"])
    args.out.mkdir(parents=True, exist_ok=True)
    onnx_path = args.out / options["onnx"]
    ids, mask = sample(model.encoder.config.vocab_size, 2, 300, 200)
    batch, seq = torch.export.Dim("batch"), torch.export.Dim("seq", min=2, max=1024)
    with torch.no_grad():
        torch.onnx.export(model, (ids, mask), onnx_path, dynamo=True, opset_version=OPSET,
                          input_names=["input_ids", "attention_mask"], output_names=["last_hidden_state"],
                          dynamic_shapes={"input_ids": {0: batch, 1: seq}, "attention_mask": {0: batch, 1: seq}},
                          external_data=True)

    session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    worst = 0.0
    for rows, length, padded_from in ((1, 40, 40), (2, 300, 90), (2, 1024, 700)):
        ids, mask = sample(model.encoder.config.vocab_size, rows, length, padded_from)
        with torch.no_grad():
            expected = model(ids, mask).numpy()
        got = session.run(None, {"input_ids": ids.numpy(), "attention_mask": mask.numpy()})[0]
        real = mask.numpy().astype(bool)
        rms = float(np.sqrt((expected[real] ** 2).mean()))
        diff = float(np.abs(expected[real] - got[real]).max()) / rms
        print(f"parity {rows}x{length} (row {rows - 1} padded from {padded_from}): max |dh| {diff:.2e} of rms {rms:.3f}")
        worst = max(worst, diff)
    if worst > MAX_HIDDEN_DIFF_OF_RMS:
        print(f"refusing the export: max |dh| is {worst:.2e} of the RMS, over {MAX_HIDDEN_DIFF_OF_RMS:.0e}",
              file=sys.stderr)
        return 1

    graph = {path.name: sha256(path) for path in sorted(args.out.glob(onnx_path.name + "*"))}
    stamp = {"repo": pinned.repo, "revision": pinned.revision, "weights_sha256": pinned.sha256,
             "graph_sha256": graph, "opset": OPSET, "torch": torch.__version__,
             "transformers": transformers.__version__, "max_hidden_diff_of_rms": worst}
    onnx_path.with_suffix(".json").write_text(json.dumps(stamp, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(stamp))
    return 0


if __name__ == "__main__":
    sys.exit(main())
