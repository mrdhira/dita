"""The parity guard: the torch-free engine against the upstream torch reference.

Fixtures are the spike's reference run (`fixtures/ref_out.json`: `rl_agent_api.RLAgent` in fp32
on CPU) over `fixtures/cases.json`. The guard needs the real weights and the exported encoder,
which the suite never downloads, so it runs where they exist: the image's parity stage and
`make parity`. Everywhere else it is skipped, and says so.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

import numpy as np

from system_one_worker.decide import parse_decide, reply, to_model
from system_one_worker.engines.decision import QTYPES, OnnxDecision, softmax, to_internal
from worker import load_registry, model_dir

MODELS_ENV = "SYSTEM_ONE_PARITY_MODELS"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANIFEST = Path(__file__).resolve().parent.parent / "models.yaml"
# Probabilities are served unrounded but read by people at four decimals; fp32 reduction-order
# noise measured 2e-6. Anything past 1e-4 is a different computation, not noise.
TOLERANCE = 1e-4
SLIDING_WINDOW = 128
# The act logits sit near +-1500 but agree with torch to 1.3e-3 at worst; a relative bound there
# could not see an error below 0.13.
ACT_ATOL = 3e-3
# The reference reports confidence rounded to four decimals.
CONFIDENCE_ATOL = 1e-4


@unittest.skipUnless(os.environ.get(MODELS_ENV), f"{MODELS_ENV} is not set: no weights here (make parity)")
class ParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        spec = load_registry(MANIFEST).get("laya-multilingual")
        directory = model_dir(Path(os.environ[MODELS_ENV]), spec)
        cls.engine = OnnxDecision(directory, spec.options)
        cls.config = json.loads((directory / spec.options["config"]).read_text(encoding="utf-8"))
        cls.cases = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))
        cls.reference = {r["name"]: r for r in json.loads((FIXTURES / "ref_out.json").read_text(encoding="utf-8"))}

    def test_the_fixtures_exercise_what_they_claim(self) -> None:
        self.assertEqual(len(self.cases), 6)
        self.assertEqual(set(self.reference), {c["name"] for c in self.cases})
        lengths = [len(s["ids"]) for r in self.reference.values() for s in r["seqs"]]
        self.assertEqual(max(lengths), 1024, "one case must fill max_len")
        self.assertGreater(max(lengths), SLIDING_WINDOW, "one case must cross the encoder's local window")
        self.assertIn("real-immich-oom", self.reference)
        self.assertTrue(any(isinstance(c["state"], dict) for c in self.cases), "a dict state")
        self.assertTrue(any(q["type"] == "score" for c in self.cases for q in c["questions"].values()), "a score")
        self.assertTrue(any(len(c["questions"]) == 1 for c in self.cases), "a single-question call")
        self.assertTrue(all("act_logits" in r for r in self.reference.values()))

    def expected(self, logits: list, qtype: str) -> np.ndarray:
        """From the checkpoint's own config, not the engine's lookup. The buckets are empty today;
        the day they are not, this must learn them."""
        self.assertEqual(self.config["temperature_by_options"], {}, "per-bucket temperatures arrived")
        return softmax(np.array(logits, np.float64) / self.config["temperature"][QTYPES[qtype]])

    def test_the_torch_free_path_reproduces_the_reference(self) -> None:
        decider = self.engine._live()
        worst = 0.0
        for case in self.cases:
            ref = self.reference[case["name"]]
            questions = [to_internal(q) for q in case["questions"].values()]
            with self.subTest(case["name"]):
                seqs = decider.sequences(case["state"], questions)
                self.assertEqual([list(s) for s in seqs], [[r["ids"], r["markers"]] for r in ref["seqs"]])
                got = self.engine.decide(case["state"], questions)
                for row, (q, answer) in enumerate(zip(questions, got, strict=True)):
                    k = len(seqs[row][1])
                    want = self.expected(ref["logits"][row][:k], q["t"])
                    diff = float(np.abs(answer.probabilities - want).max())
                    worst = max(worst, diff)
                    self.assertLessEqual(diff, TOLERANCE)
                    self.assertEqual(int(answer.probabilities.argmax()), int(want.argmax()))
                    self.assertLessEqual(abs(answer.act_probability - ref["act_softmax"][row][0]), TOLERANCE)
                    self.assertLessEqual(float(np.abs(answer.act_logits - np.array(ref["act_logits"][row])).max()),
                                         ACT_ATOL)
                    served = ref["answer"]["answers"][list(case["questions"])[row]]
                    if "confidence" in served:
                        self.assertLessEqual(abs(answer.confidence - served["confidence"]), CONFIDENCE_ATOL)
        print(f"\nparity: worst max|dp| {worst:.2e} over {len(self.cases)} cases", file=sys.stderr)
        self.assertNotIn("torch", sys.modules, "every answer above must have come without torch")

    def test_the_real_alert_answers_as_the_reference_did(self) -> None:
        case = next(c for c in self.cases if c["name"] == "real-immich-oom")
        severity, human = self.engine.decide(case["state"], [to_internal(q) for q in case["questions"].values()])
        self.assertEqual(np.round(severity.probabilities, 4).tolist(), [0.0112, 0.8507, 0.1381])
        self.assertEqual(round(float(human.probabilities[1]), 4), 0.0172)

    def test_the_contract_path_reaches_the_same_numbers(self) -> None:
        """What /decide runs: a contract request, parse_decide, to_model, the engine, reply."""
        case = next(c for c in self.cases if c["name"] == "real-immich-oom")
        ref = self.reference[case["name"]]
        body = {"text": case["state"], "questions": [
            {"name": name, "type": q["type"], "criteria": q["instructions"],
             "options": q["criteria"] if q["type"] == "choice" else ["false", "true"]}
            for name, q in case["questions"].items()]}
        text, questions = parse_decide(json.dumps(body).encode())
        decisions = self.engine.decide(text, [to_model(q) for q in questions])
        served = reply("laya-multilingual", "r", questions, decisions)["answers"]
        for row, answer in enumerate(served):
            q = questions[row]
            want = self.expected(ref["logits"][row][:len(answer["probabilities"])], q["type"])
            got = np.array(list(answer["probabilities"].values()))
            self.assertLessEqual(float(np.abs(got - want).max()), TOLERANCE, q["name"])
        self.assertEqual(round(served[0]["probabilities"]["warning"], 4), 0.8507)
        self.assertEqual(round(served[0]["confidence"], 4), ref["answer"]["answers"]["severity"]["confidence"])
        self.assertEqual(round(served[1]["probabilities"]["true"], 4), 0.0172)

    def test_neither_torch_nor_transformers_was_imported(self) -> None:
        self.assertNotIn("torch", sys.modules)
        self.assertNotIn("transformers", sys.modules)


if __name__ == "__main__":
    unittest.main()
