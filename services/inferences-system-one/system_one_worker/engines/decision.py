"""Laya's System 1 without torch: the encoder is an ONNX graph, the decision head is numpy.

A port of the upstream reference (`rl_common.build_sequence`, `DecisionModel.forward`,
`rl_agent_api.RLAgent.system_one`), held to it by the parity guard in tests/test_parity.py.
The sequence layout, the marker positions and the head's arithmetic must stay exactly the
reference's: a changed token or a reordered operation is a different model.
"""

from __future__ import annotations

import json
import math
import mmap
import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
from textinfer import InvalidRequest, open_session, pad, plan_batches
from worker import Engine, Result

THREADS_ENV = "SYSTEM_ONE_THREADS"
ONNX_DIR_ENV = "SYSTEM_ONE_ONNX_DIR"

QTYPES = {"choice": 0, "score": 1, "noul": 2}
QTYPE_NAMES = {v: k for k, v in QTYPES.items()}
_DT = {"F16": np.float16, "F32": np.float32}


class TooMuchWork(Exception):
    """The request's planned tokens exceed what can finish inside the caller's deadline."""


class DeadlineExceeded(Exception):
    """The deadline passed between batches; the work stopped and nothing is returned."""


def load_safetensors(path: Path, prefix_skip: Tuple[str, ...] = ("encoder.",)) -> Dict[str, np.ndarray]:
    """The head's tensors as fp32. The encoder's are skipped: they live in the ONNX graph."""
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        header = json.loads(f.read(n))
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    header.pop("__metadata__", None)
    out = {}
    for k, v in header.items():
        if k.startswith(prefix_skip):
            continue
        if v["dtype"] not in _DT:
            raise ValueError(f"{path}: tensor {k} is {v['dtype']}, which this reader does not handle")
        dtype = np.dtype(_DT[v["dtype"]])
        a, b = v["data_offsets"]
        out[k] = np.frombuffer(mm, dtype=dtype, count=(b - a) // dtype.itemsize,
                               offset=8 + n + a).reshape(v["shape"]).astype(np.float32)
    return out


def serialize_state(state: Any) -> str:
    return state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)


def render_options(q: Mapping[str, Any]) -> List[str]:
    """Option texts in label order. Noul is always [false, true], so p[1] is the noul."""
    t, crit = q["t"], q.get("crit")
    if t == "choice":
        return [k if not v else "%s: %s" % (k, v) for k, v in crit.items()]
    if t == "score":
        return ["level %d: %s" % (i, c) for i, c in enumerate(crit)]
    crit = crit or {}
    return ["false: " + (crit.get("false") or "no, the statement does not hold"),
            "true: " + (crit.get("true") or "yes, the statement holds")]


def temp_bucket(qtype: int, k: int) -> str:
    size = "2" if k <= 2 else "3-5" if k <= 5 else "6-10" if k <= 10 else "11+"
    return "%s:%s" % (QTYPE_NAMES[int(qtype)], size)


def to_internal(qdef: Mapping[str, Any]) -> Dict[str, Any]:
    """The upstream request shape ({type, instructions, criteria}) to the reference's own."""
    t = qdef["type"]
    crit = qdef.get("criteria")
    if t == "choice" and isinstance(crit, list):
        crit = {c: None for c in crit}
    ins = qdef["instructions"]
    return {"t": t, "ins": ins if isinstance(ins, str) else json.dumps(ins), "crit": crit}


_erf = np.frompyfunc(math.erf, 1, 1)


def gelu(x: np.ndarray) -> np.ndarray:
    """Exact erf GELU, as torch's default; the tanh approximation is a different head."""
    return (0.5 * x * (1.0 + _erf(x / math.sqrt(2.0)).astype(np.float32))).astype(np.float32)


def layer_norm(x: np.ndarray, w: np.ndarray, b: np.ndarray, eps: float = 1e-5) -> np.ndarray:
    mu = x.mean(-1, keepdims=True)
    var = ((x - mu) ** 2).mean(-1, keepdims=True)
    return (x - mu) / np.sqrt(var + eps) * w + b


def softmax(z: np.ndarray, axis: int = -1) -> np.ndarray:
    z = z - z.max(axis, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis, keepdims=True)


def confidence_from_probs(p: np.ndarray, k: int) -> float:
    """Upstream's confidence: 1 - the normalised entropy of the answer distribution."""
    if k < 2:
        return 1.0
    p = p[:k]
    ent = -(p * np.log(np.clip(p, 1e-12, 1))).sum()
    return float(1 - ent / math.log(k))


def act_features(logits: np.ndarray, marker_mask: np.ndarray) -> np.ndarray:
    """What the act head reads beside the CLS row: top-1, its margin over top-2, the entropy
    normalised by log(k), and k / 255, with k floored at 2."""
    p = softmax(logits)
    k = np.maximum(marker_mask.sum(-1), 2).astype(np.float32)
    ent = -(p * np.log(np.maximum(p, 1e-9))).sum(-1) / np.log(k)
    top2 = -np.sort(-p, -1)[:, :2]
    return np.stack([top2[:, 0], top2[:, 0] - top2[:, 1], ent, k / 255.0], -1).astype(np.float32)


@dataclass(frozen=True)
class Special:
    mask_token: str
    mask_id: int
    cls_id: int
    sep_id: int
    pad_id: int


@dataclass(frozen=True)
class Decision:
    """One question's answer: probabilities in option order, after temperature, and the act
    head's raw logits, whose softmax index 0 is `act_probability`."""

    probabilities: np.ndarray
    act_logits: np.ndarray

    @property
    def confidence(self) -> float:
        return confidence_from_probs(self.probabilities, len(self.probabilities))

    @property
    def act_probability(self) -> float:
        return float(softmax(self.act_logits.astype(np.float64))[0])


class Decider:
    """Everything except opening the files, so a test can hand it a fake session, a tokenizer
    built in memory and small head weights. Not thread-safe: `ModelManager.run` serialises."""

    def __init__(self, session: Any, tokenizer: Any, special: Special, weights: Mapping[str, np.ndarray],
                 cfg: Mapping[str, Any], max_batch_tokens: int, heads: int) -> None:
        self.tok = tokenizer
        self.special = special
        self.w = dict(weights)
        self.cfg = cfg
        self.sess = session
        self.max_batch_tokens = max_batch_tokens
        self.n_layers = len({k.split(".")[2] for k in self.w if k.startswith("head.layers.")})
        if self.n_layers != cfg["head_layers"]:
            raise ValueError("head has %d layers, config says %d" % (self.n_layers, cfg["head_layers"]))
        d = self.w["type_emb.weight"].shape[1]
        if heads < 1 or d % heads:
            raise ValueError(f"{heads} heads cannot split the head's width {d}")
        self.heads = heads
        if max_batch_tokens < cfg["max_len"]:
            raise ValueError("max_batch_tokens cannot be below max_len: one question must always fit a batch")
        self.temperature = cfg.get("temperature", [1.0, 1.0, 1.0])
        self.temperature_by_options = cfg.get("temperature_by_options", {})

    def _ids(self, text: str) -> List[int]:
        return self.tok.encode(text, add_special_tokens=False).ids

    def build_sequence(self, state: Any, q: Mapping[str, Any]) -> Tuple[List[int], List[int]]:
        """[CLS] <type> question: <instructions> [SEP] [MASK] opt0 [MASK] opt1 ... [SEP] state [SEP],
        and the index of each option's [MASK]."""
        max_len, head_max_len = self.cfg["max_len"], self.cfg["head_max_len"]
        mask_tok = self.special.mask_token
        opts = render_options(q)
        ins = str(q["ins"]).replace(mask_tok, " ")
        head_ids = self._ids("%s question: %s" % (q["t"], ins))
        opt_ids = [[self.special.mask_id] + self._ids(" " + o.replace(mask_tok, " "))[:48] for o in opts]
        opt_budget = head_max_len - sum(len(o) for o in opt_ids)
        if opt_budget < 16:
            per = max(4, (head_max_len - 16) // max(1, len(opt_ids)))
            opt_ids = [o[:per] for o in opt_ids]
            opt_budget = head_max_len - sum(len(o) for o in opt_ids)
        head_ids = head_ids[:max(8, opt_budget)]
        ids = [self.special.cls_id] + head_ids + [self.special.sep_id]
        markers = []
        for o in opt_ids:
            markers.append(len(ids))
            ids.extend(o)
        ids.append(self.special.sep_id)
        room = max(0, max_len - len(ids) - 1)
        st = self._ids(serialize_state(state).replace(mask_tok, " "))[:room]
        ids = ids + st + [self.special.sep_id]
        return ids[:max_len], [m for m in markers if m < max_len]

    def _attn_layer(self, i: int, h: np.ndarray, key_pad: np.ndarray, rows: Optional[np.ndarray] = None) -> np.ndarray:
        """nn.TransformerEncoderLayer(norm_first=True, activation=relu), eval mode.

        rows: if given, only those query rows are computed; keys and values still span the
        whole sequence, so the rows are exact.
        """
        w = lambda n: self.w["head.layers.%d.%s" % (i, n)]
        d = h.shape[-1]
        nh = self.heads
        hd = d // nh
        x = layer_norm(h, w("norm1.weight"), w("norm1.bias"))
        Wq, Wk, Wv = np.split(w("self_attn.in_proj_weight"), 3)
        bq, bk, bv = np.split(w("self_attn.in_proj_bias"), 3)
        B, L, _ = h.shape
        hq = h if rows is None else np.take_along_axis(h, rows[:, :, None], 1)
        xq = x if rows is None else np.take_along_axis(x, rows[:, :, None], 1)
        q = (xq @ Wq.T + bq).reshape(B, -1, nh, hd).transpose(0, 2, 1, 3)
        k = (x @ Wk.T + bk).reshape(B, L, nh, hd).transpose(0, 2, 1, 3)
        v = (x @ Wv.T + bv).reshape(B, L, nh, hd).transpose(0, 2, 1, 3)
        s = (q @ k.transpose(0, 1, 3, 2)) / np.float32(math.sqrt(hd))
        s = np.where(key_pad[:, None, None, :], np.float32(-np.inf), s)
        a = softmax(s) @ v
        a = a.transpose(0, 2, 1, 3).reshape(B, -1, d)
        y = hq + a @ w("self_attn.out_proj.weight").T + w("self_attn.out_proj.bias")
        z = layer_norm(y, w("norm2.weight"), w("norm2.bias"))
        z = np.maximum(z @ w("linear1.weight").T + w("linear1.bias"), 0) @ w("linear2.weight").T + w("linear2.bias")
        return (y + z).astype(np.float32)

    def forward(self, input_ids: np.ndarray, attention_mask: np.ndarray, marker_pos: np.ndarray,
                marker_mask: np.ndarray, qtype: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """(option logits with -1e4 in unused slots, act logits), as DecisionModel.forward."""
        h = self.sess.run(None, {"input_ids": input_ids, "attention_mask": attention_mask})[0]
        h = h + self.w["type_emb.weight"][qtype][:, None, :]
        pad = attention_mask == 0
        rows = np.concatenate([np.zeros((h.shape[0], 1), np.int64), marker_pos], 1)
        for i in range(self.n_layers - 1):
            h = self._attn_layer(i, h, pad)
        h = self._attn_layer(self.n_layers - 1, h, pad, rows=rows)
        pooled, m = h[:, 0], h[:, 1:]
        s = self.w
        x = layer_norm(m, s["scorer.0.weight"], s["scorer.0.bias"])
        x = gelu(x @ s["scorer.1.weight"].T + s["scorer.1.bias"])
        logits = (x @ s["scorer.3.weight"].T + s["scorer.3.bias"])[..., 0]
        logits = np.where(marker_mask, logits, np.float32(-1e4)).astype(np.float32)
        feats = act_features(logits, marker_mask)
        a = gelu(np.concatenate([pooled, feats], -1) @ s["act_head.0.weight"].T + s["act_head.0.bias"])
        act = a @ s["act_head.2.weight"].T + s["act_head.2.bias"]
        return logits, act

    def sequences(self, state: Any, questions: Sequence[Mapping[str, Any]]) -> List[Tuple[List[int], List[int]]]:
        out = []
        for index, q in enumerate(questions):
            ids, markers = self.build_sequence(state, q)
            if len(markers) != len(render_options(q)):
                raise InvalidRequest("question %d: its options do not fit in head_max_len=%d tokens"
                                     % (index, self.cfg["head_max_len"]))
            out.append((ids, markers))
        return out

    def decide(self, state: Any, questions: Sequence[Mapping[str, Any]], deadline: Optional[float] = None,
               max_tokens: Optional[int] = None) -> List[Decision]:
        """One Decision per question, in order. Batches are bounded by padded tokens; padding
        is masked, so how questions are grouped does not change any answer.

        `max_tokens` refuses the request before any encoder work; `deadline` (time.monotonic)
        is checked before each batch, and a missed one stops the work with nothing returned.
        """
        seqs = self.sequences(state, questions)
        batches = plan_batches([len(ids) for ids, _ in seqs], self.max_batch_tokens)
        planned = sum(len(group) * len(seqs[group[0]][0]) for group in batches)
        if max_tokens is not None and planned > max_tokens:
            raise TooMuchWork(f"{len(questions)} questions plan {planned} padded tokens, over the {max_tokens} "
                              "that finish inside the deadline; ask fewer questions or send shorter text")
        logits: List[Optional[np.ndarray]] = [None] * len(seqs)
        acts: List[Optional[np.ndarray]] = [None] * len(seqs)
        for done, group in enumerate(batches):
            if deadline is not None and time.monotonic() > deadline:
                answered = sum(len(g) for g in batches[:done])
                raise DeadlineExceeded(f"stopped at the deadline after {done} of {len(batches)} batches "
                                       f"({answered} of {len(questions)} questions); nothing is returned")
            inp, att = pad([seqs[i][0] for i in group], self.special.pad_id)
            K = max(len(seqs[i][1]) for i in group)
            mpos = np.zeros((len(group), K), np.int64)
            mmask = np.zeros((len(group), K), bool)
            for r, i in enumerate(group):
                markers = seqs[i][1]
                mpos[r, :len(markers)] = markers
                mmask[r, :len(markers)] = True
            qts = np.array([QTYPES[questions[i]["t"]] for i in group])
            batch_logits, batch_act = self.forward(inp, att, mpos, mmask, qts)
            for r, i in enumerate(group):
                logits[i] = batch_logits[r, :len(seqs[i][1])]
                acts[i] = batch_act[r]
        out = []
        for i, q in enumerate(questions):
            k, qt = len(seqs[i][1]), QTYPES[q["t"]]
            z = logits[i] / self.temperature_by_options.get(temp_bucket(qt, k), self.temperature[qt])
            out.append(Decision(softmax(z.astype(np.float64)), acts[i]))
        return out


def read_special(tokenizer: Any, tokenizer_config: Mapping[str, Any]) -> Special:
    ids = {}
    for key in ("mask_token", "cls_token", "sep_token", "pad_token"):
        token_id = tokenizer.token_to_id(tokenizer_config[key])
        if token_id is None:
            raise ValueError(f"{key} {tokenizer_config[key]!r} is not in the tokenizer")
        ids[key] = token_id
    return Special(tokenizer_config["mask_token"], ids["mask_token"], ids["cls_token"], ids["sep_token"],
                   ids["pad_token"])


def head_count(width: int, encoder_config: Mapping[str, Any]) -> int:
    """Upstream builds its head with max(1, d // 64) heads; the checkpoint states only the
    encoder's count. Both must agree, or the head's split is not the one it was trained with."""
    derived = max(1, width // 64)
    stated = encoder_config.get("num_attention_heads")
    if stated != derived:
        raise ValueError(f"the head splits {width} into {derived} heads, the checkpoint states {stated}")
    return derived


def check_temperature(cfg: Mapping[str, Any], weights: Mapping[str, np.ndarray]) -> None:
    """Upstream reads the temperatures from rl_agent_config.json and never from the checkpoint's
    `temperature` buffer. Serving a checkpoint whose two disagree would be serving a calibration
    nobody can say is the intended one."""
    buffer = weights.get("temperature")
    if buffer is not None and not np.allclose(buffer, cfg.get("temperature", [1.0, 1.0, 1.0])):
        raise ValueError(f"rl_agent_config.json temperature {cfg.get('temperature')} disagrees with the "
                         f"checkpoint's buffer {buffer.tolist()}")


def check_provenance(stamp_path: Path, expected_sha256: str) -> None:
    """The encoder graph and the head weights must come from the same checkpoint. A pinned
    revision bumped without rebuilding the image would otherwise pair an old encoder with a
    new head, and answer confidently."""
    try:
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read the encoder's provenance at {stamp_path}: {exc}") from None
    if stamp.get("weights_sha256") != expected_sha256:
        raise ValueError(f"the encoder at {stamp_path.parent} was exported from weights "
                         f"{stamp.get('weights_sha256')}, models.yaml pins {expected_sha256}; rebuild it")


class OnnxDecision(Engine):
    def __init__(self, model_dir: Path, options: Dict[str, Any]) -> None:
        super().__init__(model_dir, options)

        from tokenizers import Tokenizer

        for name in ("onnx", "weights", "config", "encoder_config", "tokenizer", "tokenizer_config",
                     "encoder_built_from", "max_batch_tokens"):
            if name not in options:
                raise ValueError(f"onnx_decision options are missing {name}")
        onnx_dir = Path(os.environ.get(ONNX_DIR_ENV) or model_dir / "onnx")
        onnx_path = onnx_dir / options["onnx"]
        check_provenance(onnx_path.with_suffix(".json"), options["encoder_built_from"])
        cfg = json.loads((model_dir / options["config"]).read_text(encoding="utf-8"))
        tokenizer = Tokenizer.from_file(str(model_dir / options["tokenizer"]))
        tcfg = json.loads((model_dir / options["tokenizer_config"]).read_text(encoding="utf-8"))
        weights = load_safetensors(model_dir / options["weights"])
        check_temperature(cfg, weights)
        encoder_config = json.loads((model_dir / options["encoder_config"]).read_text(encoding="utf-8"))
        heads = head_count(weights["type_emb.weight"].shape[1], encoder_config)
        session = open_session(onnx_path, int(os.environ.get(THREADS_ENV, "0") or 0))
        self._decider: Optional[Decider] = Decider(session, tokenizer, read_special(tokenizer, tcfg), weights,
                                                   cfg, int(options["max_batch_tokens"]), heads)

    def decide(self, state: Any, questions: Sequence[Mapping[str, Any]], deadline: Optional[float] = None,
               max_tokens: Optional[int] = None) -> List[Decision]:
        return self._live().decide(state, questions, deadline, max_tokens)

    def infer(self, payload: bytes) -> Result:
        raise ValueError("this worker answers with probabilities, which DIP's infer response cannot "
                         "carry; POST /decide on the HTTP port instead")

    def close(self) -> None:
        self._decider = None

    def _live(self) -> Decider:
        if self._decider is None:
            raise RuntimeError("the engine was closed")
        return self._decider
