"""Write tests/fixtures/golden_head.npz: upstream's decision head, in torch, on fake-size weights.

The offline suite cannot run the real model, but it can hold the numpy head to torch's
arithmetic: the weights are `tests.support.head_weights()` (seeded numpy, so the test rebuilds
them exactly), the head below is `rl_common.DecisionModel.forward` after the encoder, line for
line, and the hidden states stand in for the encoder's. Runs where torch exists (`make golden`).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

SERVICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE))

from tests.support import D, head_weights  # noqa: E402

OUT = SERVICE / "tests" / "fixtures" / "golden_head.npz"


class Head(nn.Module):
    """DecisionModel minus its encoder, built as upstream builds it."""

    def __init__(self, d: int, head_layers: int = 2, n_act: int = 2) -> None:
        super().__init__()
        nhead = max(1, d // 64)
        layer = nn.TransformerEncoderLayer(d, nhead, 4 * d, 0.1, batch_first=True, norm_first=True)
        self.head = nn.TransformerEncoder(layer, head_layers, enable_nested_tensor=False)
        self.type_emb = nn.Embedding(3, d)
        self.scorer = nn.Sequential(nn.LayerNorm(d), nn.Linear(d, d), nn.GELU(), nn.Linear(d, 1))
        self.act_head = nn.Sequential(nn.Linear(d + 4, 256), nn.GELU(), nn.Linear(256, n_act))

    def forward(self, h, attention_mask, marker_pos, marker_mask, qtype):
        h = h + self.type_emb(qtype)[:, None, :]
        pad = ~attention_mask.bool()
        for layer in self.head.layers:
            h = layer(h, src_key_padding_mask=pad)
        idx = marker_pos.clamp(min=0)[:, :, None].expand(-1, -1, h.size(-1))
        m = torch.gather(h, 1, idx)
        logits = self.scorer(m).squeeze(-1).float()
        logits = logits.masked_fill(~marker_mask, -1e4)
        p = torch.softmax(logits.detach(), -1)
        k = marker_mask.sum(-1).clamp(min=2).float()
        ent = -(p * torch.log(p.clamp_min(1e-9))).sum(-1) / torch.log(k)
        top2 = p.topk(2, -1).values
        feats = torch.stack([top2[:, 0], top2[:, 0] - top2[:, 1], ent, k / 255.0], -1)
        pooled = h[:, 0].float()
        return logits, self.act_head(torch.cat([pooled, feats], -1))


def main() -> int:
    head = Head(D).eval()
    head.load_state_dict({k: torch.from_numpy(v) for k, v in head_weights().items()}, strict=True)
    rng = np.random.default_rng(11)
    h = rng.standard_normal((3, 14, D)).astype(np.float32)
    attention_mask = np.ones((3, 14), np.int64)
    attention_mask[1, 10:] = 0
    attention_mask[2, 6:] = 0
    marker_pos = np.array([[3, 5, 7, 9], [2, 4, 0, 0], [1, 2, 3, 0]], np.int64)
    marker_mask = np.array([[1, 1, 1, 1], [1, 1, 0, 0], [1, 1, 1, 0]], bool)
    qtype = np.array([0, 2, 1], np.int64)
    with torch.no_grad():
        logits, act = head(torch.from_numpy(h), torch.from_numpy(attention_mask), torch.from_numpy(marker_pos),
                           torch.from_numpy(marker_mask), torch.from_numpy(qtype))
    np.savez_compressed(OUT, h=h, attention_mask=attention_mask, marker_pos=marker_pos, marker_mask=marker_mask,
                        qtype=qtype, logits=logits.numpy(), act_logits=act.numpy(),
                        torch_version=np.array(torch.__version__))
    print(f"wrote {OUT.relative_to(SERVICE)} with torch {torch.__version__}: logits {logits.numpy().round(4).tolist()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
