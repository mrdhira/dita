"""Tokenised text through an ONNX graph, in batches whose padded cost has a ceiling.

The ceiling is what bounds peak memory: attention grows with the square of the padded
length, so a request is split by tokens, never by count.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

FEEDABLE = ("input_ids", "attention_mask", "token_type_ids", "position_ids")


def open_session(path: Path, threads: int = 0) -> Any:
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.log_severity_level = 3
    # The arena keeps every high-water mark: one long batch would pin gigabytes for good.
    options.enable_cpu_mem_arena = False
    options.intra_op_num_threads = threads
    return ort.InferenceSession(str(path), sess_options=options, providers=["CPUExecutionProvider"])


def plan_batches(lengths: Sequence[int], max_batch_tokens: int) -> List[List[int]]:
    """Longest first, so each batch pads to a length its members nearly share, and no batch
    costs more than `max_batch_tokens` once padded. An input always gets a batch."""
    batches: List[List[int]] = []
    for index in sorted(range(len(lengths)), key=lambda i: -lengths[i]):
        current = batches[-1] if batches else None
        if current and (len(current) + 1) * lengths[current[0]] <= max_batch_tokens:
            current.append(index)
        else:
            batches.append([index])
    return batches


def pad(sequences: Sequence[Sequence[int]], pad_id: int) -> Tuple[np.ndarray, np.ndarray]:
    width = max(len(ids) for ids in sequences)
    input_ids = np.full((len(sequences), width), pad_id, dtype=np.int64)
    mask = np.zeros((len(sequences), width), dtype=np.int64)
    for row, ids in enumerate(sequences):
        input_ids[row, : len(ids)] = ids
        mask[row, : len(ids)] = 1
    return input_ids, mask


def feedable(name: str) -> bool:
    return name in FEEDABLE or name.startswith("past_key_values.")


def feeds(inputs: Sequence[Tuple[str, Sequence[Any]]], input_ids: np.ndarray,
          mask: np.ndarray) -> Dict[str, np.ndarray]:
    """What each graph input a text model can ask for should hold, for right-padded rows."""
    fed: Dict[str, np.ndarray] = {}
    for name, shape in inputs:
        if name == "input_ids":
            fed[name] = input_ids
        elif name == "attention_mask":
            fed[name] = mask
        elif name == "token_type_ids":
            fed[name] = np.zeros_like(input_ids)
        elif name == "position_ids":
            fed[name] = np.maximum(np.cumsum(mask, axis=1) - 1, 0)
        elif name.startswith("past_key_values."):
            # A decoder exported with a KV cache: an empty past is a plain forward pass.
            fed[name] = np.zeros((input_ids.shape[0], shape[1], 0, shape[3]), dtype=np.float32)
        else:
            raise ValueError(f"the graph wants an input this adapter cannot feed: {name}")
    return fed
