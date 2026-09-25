"""Regenerate tests/fixtures/ref_out.json: upstream's own reference code, torch, on the pinned weights.

The parity guard is only as good as its reference, so the reference is reproducible from here:
upstream's `rl_agent_api.py` and `rl_common.py` are fetched at an immutable revision and refused
unless their sha256 matches, the weights come through the worker's fetcher and models.yaml, and
`RLAgent.system_one` runs unmodified over tests/fixtures/cases.json in fp32 on CPU. A forward
hook records the raw option and act logits, which the answer alone rounds away. Runs where torch
exists (`make reference`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent
FIXTURES = SERVICE / "tests" / "fixtures"
UPSTREAM = "https://huggingface.co/convaiinnovations/laya/resolve/5e7b2b1b8ca2ecdd3f2322d94069c9b6ce7e844b"
CODE = {
    "rl_common.py": "8d83611d480c971d640a7b7d3aa2f2219c5e8455e9cc2329fd073681bd8be23e",
    "rl_agent_api.py": "be3b46819c9999c3ef88e0f2ecf6d3ab1cdfed1d9a8b466fc89811e34d44031b",
}


def fetch_upstream(into: Path) -> None:
    for name, pinned in CODE.items():
        with urllib.request.urlopen(f"{UPSTREAM}/{name}", timeout=60) as response:
            body = response.read()
        actual = hashlib.sha256(body).hexdigest()
        if actual != pinned:
            raise SystemExit(f"{name} has sha256 {actual}, pinned {pinned}; refusing to run it")
        (into / name).write_bytes(body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=FIXTURES / "ref_out.json")
    args = parser.parse_args()

    from worker import ensure_model, load_registry, model_dir

    spec = load_registry(SERVICE / "models.yaml").get("laya-multilingual")
    ensure_model(args.models_dir, spec)
    directory = model_dir(args.models_dir, spec)

    with tempfile.TemporaryDirectory() as code:
        fetch_upstream(Path(code))
        sys.path.insert(0, code)
        os.environ.setdefault("USE_TF", "0")
        import torch
        from rl_agent_api import RLAgent
        from rl_common import build_sequence

        agent = RLAgent(str(directory), device="cpu")
        captured = {}
        agent.model.register_forward_hook(
            lambda m, i, o: captured.update(logits=o[0].float().numpy().copy(), act=o[1].float().numpy().copy()))
        out = []
        for case in json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8")):
            answer = agent.system_one(case["state"], case["questions"])
            seqs = []
            for qdef in case["questions"].values():
                q = agent._to_internal(qdef)
                ids, markers = build_sequence(agent.tok, case["state"], q, agent.cfg["max_len"], agent.cfg["head_max_len"])
                seqs.append({"ids": ids, "markers": markers})
            out.append({"name": case["name"], "answer": answer, "logits": captured["logits"].tolist(),
                        "act_softmax": torch.softmax(torch.from_numpy(captured["act"]), -1).numpy().tolist(),
                        "act_logits": captured["act"].tolist(), "seqs": seqs})
            print(case["name"], json.dumps(answer["answers"], ensure_ascii=False))
    args.out.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {args.out} from torch {torch.__version__}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
